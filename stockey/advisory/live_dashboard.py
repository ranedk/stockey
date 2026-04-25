from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd

from advisory.action_recommender import build_action_recommendations
from advisory.action_recommender import TABLE_NAME as ACTIONS_TABLE
from advisory.dashboard import build_dashboard
from advisory.execution_engine import EXECUTION_TABLE
from advisory.portfolio_engine import PORTFOLIO_TABLE, derive_thesis_policy
from advisory.position_lifecycle import LIFECYCLE_TABLE, REBALANCE_TABLE
from advisory.setup_registry import load_setup_registry
from advisory.sync_state import load_sync_states
from utils.db import sql_to_df
from utils.display_time import DISPLAY_TZ, to_display_timestamp, to_display_value
from utils.sync import parse_datetime_arg


ALERTS_TABLE = "advisory_live_watch_alerts"
DEFAULT_OUTPUT_DIR = Path("live_dashboard")
DEFAULT_OPERATOR_FEED_PATH = DEFAULT_OUTPUT_DIR / "operator_feed.json"
DEFAULT_CRON_LOG_DIR = Path("logs/cron")


def _setup_metadata() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for setup in load_setup_registry():
        setup_id = str(setup.get("setup_id") or "").upper()
        if not setup_id:
            continue
        out[setup_id] = {
            "setup_name": setup.get("setup_name"),
            "setup_family": setup.get("setup_family"),
            "holding_horizon_note": setup.get("holding_horizon_note"),
            "screeners": setup.get("screeners") or setup.get("screener_slugs") or ([] if not setup.get("screener_slug") else [setup.get("screener_slug")]),
        }
    return out


def _table_columns(table_name: str) -> set[str]:
    try:
        df = sql_to_df(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            """,
            params=(table_name,),
        )
    except Exception:
        return set()
    if df.empty or "column_name" not in df.columns:
        return set()
    return {str(value) for value in df["column_name"].dropna().astype(str)}


def _json_ready(value: Any) -> Any:
    return to_display_value(value)


def _display_dashboard_timestamp(value: Any, *, prefer_date_for_midnight_utc: bool = False) -> str | None:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    if prefer_date_for_midnight_utc and ts.hour == 0 and ts.minute == 0 and ts.second == 0 and ts.microsecond == 0:
        return ts.tz_convert(DISPLAY_TZ).strftime("%Y-%m-%d")
    return ts.tz_convert(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _normalize_utc_arg_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _as_text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _as_float(value: Any) -> float | None:
    out = pd.to_numeric(value, errors="coerce")
    if pd.isna(out):
        return None
    return float(out)


def _parse_json_blob(value: Any) -> dict[str, Any]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _format_summary_lines(rows: list[dict[str, Any]], *, limit: int = 2) -> str | None:
    if not rows:
        return None
    parts: list[str] = []
    for row in rows[:limit]:
        published_on = _as_text(row.get("published_on"))
        subject = _as_text(row.get("subject"))
        summary = _as_text(row.get("concise_summary_text"))
        bits = [bit for bit in [published_on, subject, summary] if bit]
        if bits:
            parts.append(" | ".join(bits))
    return " || ".join(parts) if parts else None


def _compact_count_map(value: Any, *, limit: int = 4) -> str | None:
    if not isinstance(value, dict) or not value:
        return None
    parts: list[str] = []
    for key, count in list(value.items())[:limit]:
        key_text = _as_text(key)
        if not key_text:
            continue
        parts.append(f"{key_text}: {count}")
    return " | ".join(parts) if parts else None


def _setup_dashboard_map(dashboard_df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if dashboard_df.empty or "setup_id" not in dashboard_df.columns:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for _, row in dashboard_df.iterrows():
        setup_id = _as_text(row.get("setup_id"))
        if not setup_id:
            continue
        out[setup_id.upper()] = row.to_dict()
    return out


def _market_context_text(setup_snapshot: dict[str, Any]) -> str | None:
    regime_name = _as_text(setup_snapshot.get("regime_name"))
    overlay_name = _as_text(setup_snapshot.get("overlay_name"))
    overlay_reason = _as_text(setup_snapshot.get("overlay_reason"))
    pieces: list[str] = []
    if regime_name:
        pieces.append(f"Base regime: {regime_name}.")
    if overlay_name:
        pieces.append(f"Overlay: {overlay_name}.")
    if overlay_reason:
        pieces.append(f"Overlay reason: {overlay_reason}")
    return " ".join(pieces) if pieces else None


def _screener_context_text(setup_snapshot: dict[str, Any], setup_meta: dict[str, Any]) -> str | None:
    active_screeners = setup_snapshot.get("active_screeners") or setup_meta.get("screeners") or []
    theme_ids = setup_snapshot.get("active_theme_ids") or []
    universe_count = setup_snapshot.get("screener_universe_count")
    candidate_by_screener = _compact_count_map(setup_snapshot.get("candidate_count_by_screener"))
    pieces: list[str] = []
    if active_screeners:
        pieces.append(f"Active screeners: {', '.join(str(value) for value in active_screeners)}.")
    if theme_ids:
        pieces.append(f"Theme drivers: {', '.join(str(value) for value in theme_ids)}.")
    if universe_count is not None and not pd.isna(universe_count):
        pieces.append(f"Screener universe: {int(universe_count)} names.")
    if candidate_by_screener:
        pieces.append(f"Candidate contribution: {candidate_by_screener}.")
    return " ".join(pieces) if pieces else None


def _setup_context_text(setup_snapshot: dict[str, Any], setup_meta: dict[str, Any]) -> str | None:
    setup_family = _as_text(setup_meta.get("setup_family"))
    horizon_note = _as_text(setup_meta.get("holding_horizon_note"))
    top_rejection = _as_text(setup_snapshot.get("top_rejection_reason"))
    candidate_count = setup_snapshot.get("candidate_count")
    watch_count = setup_snapshot.get("watchlist_count")
    portfolio_count = setup_snapshot.get("portfolio_count")
    funnel_bits: list[str] = []
    if candidate_count is not None and not pd.isna(candidate_count):
        funnel_bits.append(f"candidates {int(candidate_count)}")
    if watch_count is not None and not pd.isna(watch_count):
        funnel_bits.append(f"watch {int(watch_count)}")
    if portfolio_count is not None and not pd.isna(portfolio_count):
        funnel_bits.append(f"portfolio {int(portfolio_count)}")
    pieces: list[str] = []
    if setup_family:
        pieces.append(f"Setup family: {setup_family}.")
    if horizon_note:
        pieces.append(f"Holding horizon: {horizon_note}.")
    if funnel_bits:
        pieces.append(f"Funnel now: {', '.join(funnel_bits)}.")
    if top_rejection:
        pieces.append(f"Most common rejection in this setup: {top_rejection}.")
    return " ".join(pieces) if pieces else None


def _summarize_exit_rules(raw_value: Any) -> str | None:
    if raw_value is None or (isinstance(raw_value, float) and pd.isna(raw_value)):
        return None
    parsed = raw_value
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except Exception:
            return raw_value
    if not isinstance(parsed, list):
        return str(parsed)
    codes = [str(item.get("code")) for item in parsed if isinstance(item, dict) and item.get("code")]
    return ", ".join(codes) if codes else None


def _build_bucket_summary(row: pd.Series) -> str | None:
    raw_bucket = row.get("thesis_bucket")
    if raw_bucket is None or pd.isna(raw_bucket):
        return None
    bucket = str(raw_bucket).strip().upper()
    if not bucket or bucket == "NAN":
        return None
    bucket_reason = row.get("bucket_reason")
    bucket_reason_text = None if bucket_reason is None or pd.isna(bucket_reason) else str(bucket_reason).strip()
    if bucket == "TARGET":
        review = row.get("target_review_date")
        review_text = None if review is None or pd.isna(review) else str(review)
        return f"{bucket}: {bucket_reason_text or '-'}" + (f" Review {review_text}." if review_text else "")
    if bucket == "TIME_HORIZON":
        horizon = row.get("expected_horizon_days")
        horizon_type = row.get("horizon_type")
        horizon_type_text = None if horizon_type is None or pd.isna(horizon_type) else str(horizon_type).strip()
        horizon_text = f"{int(horizon)}d" if horizon is not None and not pd.isna(horizon) else (horizon_type_text or "-")
        return f"{bucket}: {bucket_reason_text or '-'} Window {horizon_text}."
    dependency_reason = row.get("data_dependency_reason")
    dependency_text = None if dependency_reason is None or pd.isna(dependency_reason) else str(dependency_reason).strip()
    return f"{bucket}: {bucket_reason_text or dependency_text or '-'}"


def _active_exit_from_action(next_action: Any) -> str | None:
    action = str(next_action or "").strip().lower()
    if action == "exit_invalidation":
        return "INVALIDATION_HIT"
    if action == "exit_stop":
        return "STOP_HIT"
    if action == "review_stale":
        return "STALE_REVIEW"
    if action == "trim_winner":
        return "TRIM_WINNER"
    if action == "tighten_stop":
        return "TIGHTEN_STOP"
    return None


def _format_exit_strategy(row: pd.Series | dict[str, Any]) -> str | None:
    stop_price = _as_float(row.get("stop_price"))
    invalidation_price = _as_float(row.get("invalidation_price"))
    recommended_stop_price = _as_float(row.get("recommended_stop_price"))
    action_fraction = _as_float(row.get("action_fraction"))
    execution_mode = _as_text(row.get("execution_mode"))
    suggested_action = (_as_text(row.get("suggested_action")) or _as_text(row.get("status")) or "").lower()
    policy = _as_text(row.get("exit_policy_summary")) or _summarize_exit_rules(row.get("exit_event_rules_json"))
    parts: list[str] = []
    if invalidation_price is not None:
        parts.append(f"Invalidation {invalidation_price:.2f}")
    if stop_price is not None and (invalidation_price is None or abs(stop_price - invalidation_price) > 1e-9):
        parts.append(f"Stop {stop_price:.2f}")
    if recommended_stop_price is not None and (stop_price is None or abs(recommended_stop_price - stop_price) > 1e-9):
        parts.append(f"Recommended stop {recommended_stop_price:.2f}")
    if action_fraction is not None and 0 < action_fraction < 1:
        label = "Add size" if suggested_action == "add_on_pullback" else "Scale-out"
        parts.append(f"{label} {round(action_fraction * 100.0, 1):.1f}%")
    if execution_mode:
        parts.append(f"Execution {execution_mode}")
    if policy:
        parts.append(policy)
    return " | ".join(parts) if parts else None


def _format_action_summary(row: pd.Series | dict[str, Any]) -> str | None:
    action = (_as_text(row.get("suggested_action")) or _as_text(row.get("status")) or "").strip().lower()
    action_fraction = _as_float(row.get("action_fraction"))
    recommended_stop_price = _as_float(row.get("recommended_stop_price"))
    execution_mode = _as_text(row.get("execution_mode"))
    if not action:
        return None
    pct_text = f"{round(action_fraction * 100.0, 1):.1f}%" if action_fraction is not None and 0 < action_fraction <= 1 else None
    if action == "add_on_pullback":
        base = "Add on pullback."
        if pct_text:
            base += f" Add roughly {pct_text} of current live size."
    elif action == "trim_winner":
        base = "Trim winner."
        if pct_text:
            base += f" Reduce roughly {pct_text} of current live size."
    elif action == "tighten_stop":
        base = "Tighten stop."
        if recommended_stop_price is not None:
            base += f" Move stop baseline to {recommended_stop_price:.2f}."
    elif action.startswith("exit_"):
        base = "Full exit."
        if action == "exit_invalidation":
            base += " Thesis invalidation was hit."
        elif action == "exit_stop":
            base += " Stop was hit."
        elif action == "exit_emergency":
            base += " Emergency technical failure."
        elif action == "exit_technical_failure":
            base += " Technical thesis failure."
    else:
        base = action.replace("_", " ").title() + "."
    if execution_mode:
        base += f" Execution mode: {execution_mode}."
    return base


def _format_execution_intent(row: pd.Series | dict[str, Any]) -> str | None:
    transaction_type = (_as_text(row.get("transaction_type")) or "").upper()
    quantity = pd.to_numeric(row.get("quantity"), errors="coerce")
    execution_status = _as_text(row.get("execution_status"))
    execution_reason = _as_text(row.get("execution_reason"))
    order_type = (_as_text(row.get("order_type")) or "").upper()
    product_type = (_as_text(row.get("product_type")) or "").upper()
    reference_price = _as_float(row.get("reference_price"))
    if not any([transaction_type, execution_status, execution_reason, order_type, product_type, reference_price is not None]):
        return None
    parts: list[str] = []
    if transaction_type:
        qty_text = f" {int(quantity)}" if pd.notna(quantity) else ""
        parts.append(f"{transaction_type}{qty_text}")
    if order_type:
        parts.append(order_type)
    if product_type:
        parts.append(product_type)
    if reference_price is not None:
        parts.append(f"Ref {reference_price:.2f}")
    if execution_status:
        parts.append(f"Status {execution_status}")
    if execution_reason:
        parts.append(execution_reason)
    return " | ".join(parts) if parts else None


def _recommendation_reason(row: pd.Series | dict[str, Any]) -> str | None:
    for key in ["lifecycle_reason", "portfolio_reason", "watch_reason_detail", "candidate_state", "next_action_reason"]:
        text = _as_text(row.get(key))
        if text:
            return text
    return None


def _recommendation_detail(row: pd.Series | dict[str, Any]) -> str | None:
    for key in ["execution_notes", "watch_reason_detail", "next_action_reason", "bucket_summary", "bucket_reason", "last_state_transition_hint"]:
        text = _as_text(row.get(key))
        if text:
            return text
    return None


def _technical_state_summary(row: pd.Series | dict[str, Any]) -> str | None:
    state = _as_text(row.get("technical_state"))
    trigger = _as_text(row.get("technical_trigger_type"))
    note = _as_text(row.get("technical_trigger_note"))
    score = _as_float(row.get("technical_score"))
    pieces: list[str] = []
    if state:
        pieces.append(f"Technical state: {state}.")
    if trigger:
        pieces.append(f"Trigger: {trigger}.")
    if score is not None:
        pieces.append(f"Composite technical score: {round(score * 100.0, 1):.1f}/100.")
    if note:
        pieces.append(note)
    return " ".join(pieces) if pieces else None


def _technical_bucket_label(label: str, value: float) -> str:
    if label == "Trend":
        if value >= 20:
            return "strong uptrend"
        if value >= 15:
            return "tradable uptrend"
        if value >= 10:
            return "mixed trend"
        return "weak trend"
    if label == "Structure":
        if value >= 24:
            return "constructive base"
        if value >= 18:
            return "acceptable structure"
        if value >= 12:
            return "loose structure"
        return "poor structure"
    if label == "Participation":
        if value >= 15:
            return "strong participation"
        if value >= 10:
            return "acceptable participation"
        if value >= 6:
            return "mixed participation"
        return "weak participation"
    if label == "Relative strength":
        if value >= 12:
            return "clear leadership"
        if value >= 8:
            return "acceptable leadership"
        if value >= 5:
            return "average leadership"
        return "lagging leadership"
    if label == "Tradability":
        if value >= 8:
            return "easy to execute"
        if value >= 6:
            return "acceptable to execute"
        if value >= 4:
            return "execution needs care"
        return "poor tradability"
    return ""


def _technical_score_breakdown(row: pd.Series | dict[str, Any]) -> str | None:
    items: list[str] = []
    mapping = [
        ("Trend", "technical_trend_score"),
        ("Structure", "technical_structure_score"),
        ("Participation", "technical_participation_score"),
        ("Relative strength", "technical_relative_strength_score"),
        ("Tradability", "technical_tradability_score"),
    ]
    for label, key in mapping:
        value = _as_float(row.get(key))
        if value is None:
            continue
        items.append(f"{label} {value:.1f} ({_technical_bucket_label(label, value)})")
    return " | ".join(items) if items else None


def _technical_interpretation(row: pd.Series | dict[str, Any]) -> str | None:
    scores = {
        "trend": _as_float(row.get("technical_trend_score")),
        "structure": _as_float(row.get("technical_structure_score")),
        "participation": _as_float(row.get("technical_participation_score")),
        "relative strength": _as_float(row.get("technical_relative_strength_score")),
        "tradability": _as_float(row.get("technical_tradability_score")),
    }
    valid_scores = {key: value for key, value in scores.items() if value is not None}
    if not valid_scores:
        return None
    strengths = sorted(valid_scores.items(), key=lambda item: item[1], reverse=True)
    weaknesses = sorted(valid_scores.items(), key=lambda item: item[1])
    strong_text = ", ".join(name for name, value in strengths[:2] if value >= 8.0)
    weak_text = ", ".join(name for name, value in weaknesses[:2] if value < 8.0)
    parts: list[str] = []
    if strong_text:
        parts.append(f"Strongest areas: {strong_text}.")
    if weak_text:
        parts.append(f"Weakest areas: {weak_text}.")
    return " ".join(parts) if parts else None


def _merge_reason_detail(*values: Any) -> str | None:
    parts: list[str] = []
    for value in values:
        text = _as_text(value)
        if text and text not in parts:
            parts.append(text)
    return " ".join(parts) if parts else None


def load_portfolio_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 25) -> pd.DataFrame:
    available = _table_columns(PORTFOLIO_TABLE)
    select_columns = [
        "published_on",
        "asof_date",
        "planned_at",
        "setup_id",
        "symbol",
        "portfolio_status",
        "portfolio_reason",
        "stop_price",
        "invalidation_price",
        "approved_allocation_inr",
        "requested_allocation_inr",
        "priority_score",
    ]
    if "invest_score_pct" in available:
        select_columns.append("invest_score_pct")
    for optional in ["thesis_bucket", "bucket_reason", "target_review_date", "expected_horizon_days", "exit_event_rules_json"]:
        if optional in available:
            select_columns.insert(4 if optional == "thesis_bucket" else len(select_columns), optional)
    clauses = ["1 = 1"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_portfolio_orders)")
    df = sql_to_df(
        f"""
        SELECT
            {", ".join(select_columns)}
        FROM {PORTFOLIO_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY
            CASE WHEN portfolio_status = 'approved' THEN 0 ELSE 1 END,
            approved_allocation_inr DESC NULLS LAST,
            priority_score DESC NULLS LAST,
            symbol
        LIMIT {int(limit)}
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    for column in ["published_on", "asof_date", "planned_at", "target_review_date"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df = df.sort_values(
        ["symbol", "portfolio_status", "approved_allocation_inr", "priority_score", "published_on", "setup_id"],
        ascending=[True, True, False, False, False, True],
        kind="stable",
    ).drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)
    setup_meta = _setup_metadata()
    for idx, row in df.iterrows():
        setup_id = str(row.get("setup_id") or "").upper()
        meta = setup_meta.get(setup_id, {})
        enriched = pd.Series({**meta, **row.to_dict()})
        derived = derive_thesis_policy(enriched)
        for key, value in derived.items():
            if key not in df.columns:
                df[key] = None
            current = df.at[idx, key]
            if current is None or (isinstance(current, float) and pd.isna(current)):
                df.at[idx, key] = value
    df["bucket_summary"] = df.apply(lambda row: _build_bucket_summary(row), axis=1)
    df["exit_policy_summary"] = df.get("exit_event_rules_json", pd.Series(dtype="object")).map(_summarize_exit_rules)
    return df


def load_watchlist_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 50) -> pd.DataFrame:
    clauses = ["1 = 1"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_watchlist)")
    df = sql_to_df(
        f"""
        SELECT
            asof_date,
            setup_id,
            setup_name,
            symbol,
            rank,
            watch_status,
            current_state,
            candidate_state,
            watch_reason_detail,
            attractive_price_low,
            attractive_price_high,
            invalidation_price,
            news_overlay,
            source_screener_slug,
            state_updated_at,
            last_state_transition_hint
        FROM advisory_watchlist
        WHERE {' AND '.join(clauses)}
        ORDER BY state_updated_at DESC NULLS LAST, symbol
        LIMIT {int(limit)}
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    for column in ["asof_date", "state_updated_at"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    if "rank" not in df.columns:
        df["rank"] = pd.NA
    df["rank"] = pd.to_numeric(df["rank"], errors="coerce")
    return df.sort_values(
        ["symbol", "state_updated_at", "rank", "setup_id"],
        ascending=[True, False, True, True],
        kind="stable",
    ).drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)


def load_candidate_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 50) -> pd.DataFrame:
    clauses = ["1 = 1"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_candidates)")
    df = sql_to_df(
        f"""
        SELECT
            asof_date,
            setup_id,
            setup_name,
            symbol,
            candidate_state,
            technical_state,
            technical_trigger_type,
            technical_trigger_note,
            technical_trend_score,
            technical_structure_score,
            technical_participation_score,
            technical_relative_strength_score,
            technical_tradability_score,
            setup_score,
            technical_score,
            fundamental_score,
            event_score,
            attractive_price_low,
            attractive_price_high,
            invalidation_price,
            source_screener_slug,
            news_overlay
        FROM advisory_candidates
        WHERE {' AND '.join(clauses)}
        ORDER BY
            CASE
                WHEN candidate_state = 'PASS_NOW' THEN 0
                WHEN candidate_state LIKE 'WATCH%%' THEN 1
                WHEN candidate_state = 'ABSTAIN' THEN 2
                ELSE 3
            END,
            setup_score DESC NULLS LAST,
            symbol
        LIMIT {int(limit)}
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    if "asof_date" in df.columns:
        df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce")
    return df


def _safe_frame_loader(loader, *args, **kwargs) -> pd.DataFrame:
    try:
        return loader(*args, **kwargs)
    except Exception as exc:
        print(f"[advisory.live_dashboard] section loader failed {loader.__name__}: {exc}", flush=True)
        return pd.DataFrame()


def _safe_list_loader(loader, *args, **kwargs) -> list[dict[str, Any]]:
    try:
        return loader(*args, **kwargs)
    except Exception as exc:
        print(f"[advisory.live_dashboard] section loader failed {loader.__name__}: {exc}", flush=True)
        return []


def load_lifecycle_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 25) -> pd.DataFrame:
    available = _table_columns(LIFECYCLE_TABLE)
    select_columns = [
        "published_on",
        "asof_date",
        "setup_id",
        "symbol",
        "unique_id",
        "position_status",
        "next_action",
        "next_action_reason",
        "current_price",
        "entry_price",
        "pnl_pct",
        "approved_allocation_inr",
    ]
    for optional in ["thesis_bucket", "active_exit_condition", "exit_condition_status", "bucket_status_note"]:
        if optional in available:
            select_columns.insert(4 if optional == "thesis_bucket" else len(select_columns), optional)
    clauses = ["1 = 1"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_position_lifecycle)")
    df = sql_to_df(
        f"""
        SELECT
            {", ".join(select_columns)}
        FROM advisory_position_lifecycle
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC, symbol
        LIMIT {int(limit)}
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    for column in ["published_on", "asof_date"]:
        df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df = df.sort_values(
        ["symbol", "published_on", "approved_allocation_inr", "setup_id"],
        ascending=[True, False, False, True],
        kind="stable",
    ).drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)
    portfolio_fallback = load_portfolio_rows(asof_date=asof_date, limit=max(limit * 3, 100))
    if not portfolio_fallback.empty and "unique_id" not in portfolio_fallback.columns:
        clauses = ["1 = 1"]
        params: list[object] = []
        if asof_date is not None:
            clauses.append("asof_date = %s")
            params.append(asof_date)
        else:
            clauses.append(f"asof_date = (SELECT MAX(asof_date) FROM {PORTFOLIO_TABLE})")
        fallback_df = sql_to_df(
            f"""
            SELECT unique_id, setup_id, symbol, thesis_bucket, bucket_reason, target_review_date, expected_horizon_days, exit_event_rules_json
            FROM {PORTFOLIO_TABLE}
            WHERE {' AND '.join(clauses)}
            """,
            params=tuple(params) if params else None,
        )
        if not fallback_df.empty:
            portfolio_fallback = portfolio_fallback.merge(
                fallback_df,
                how="left",
                on=["setup_id", "symbol"],
                suffixes=("", "_fallback"),
            )
    if not portfolio_fallback.empty:
        fallback_cols = [
            "thesis_bucket",
            "bucket_reason",
            "target_review_date",
            "expected_horizon_days",
            "exit_event_rules_json",
            "invest_score_pct",
            "bucket_summary",
            "exit_policy_summary",
        ]
        available_cols = [col for col in ["unique_id", "setup_id", "symbol", *fallback_cols] if col in portfolio_fallback.columns]
        fallback_map = portfolio_fallback[available_cols].drop_duplicates(subset=[col for col in ["unique_id", "setup_id", "symbol"] if col in available_cols], keep="first")
        join_keys = [col for col in ["unique_id", "setup_id", "symbol"] if col in df.columns and col in fallback_map.columns]
        if join_keys:
            df = df.merge(fallback_map, how="left", on=join_keys, suffixes=("", "_portfolio"))
            for column in fallback_cols:
                portfolio_col = f"{column}_portfolio"
                if portfolio_col in df.columns:
                    if column not in df.columns:
                        df[column] = None
                    df[column] = df[column].where(df[column].notna(), df[portfolio_col])
                    df = df.drop(columns=[portfolio_col])
    setup_meta = _setup_metadata()
    for idx, row in df.iterrows():
        if row.get("thesis_bucket") is None or pd.isna(row.get("thesis_bucket")):
            setup_id = str(row.get("setup_id") or "").upper()
            meta = setup_meta.get(setup_id, {})
            if meta:
                derived = derive_thesis_policy(pd.Series({**meta, **row.to_dict()}))
                for key, value in derived.items():
                    if key not in df.columns:
                        df[key] = None
                    current = df.at[idx, key]
                    if current is None or (isinstance(current, float) and pd.isna(current)):
                        df.at[idx, key] = value
    if "active_exit_condition" not in df.columns:
        df["active_exit_condition"] = None
    if "exit_condition_status" not in df.columns:
        df["exit_condition_status"] = None
    if "bucket_status_note" not in df.columns:
        df["bucket_status_note"] = None
    df["active_exit_condition"] = df.apply(
        lambda row: row.get("active_exit_condition")
        or ("ENTRY_PRICE_MISSING" if str(row.get("next_action") or "").strip().lower() == "review_manual" else _active_exit_from_action(row.get("next_action"))),
        axis=1,
    )
    df["exit_condition_status"] = df.apply(
        lambda row: row.get("exit_condition_status") or ("triggered" if row.get("active_exit_condition") else "active"),
        axis=1,
    )
    df["bucket_summary"] = df.apply(lambda row: _build_bucket_summary(row), axis=1)
    df["exit_policy_summary"] = df.get("exit_event_rules_json", pd.Series(dtype="object")).map(_summarize_exit_rules)
    return df


def load_recent_watch_events(*, limit: int = 25) -> pd.DataFrame:
    df = sql_to_df(
        f"""
        SELECT
            published_on,
            asof_date,
            setup_id,
            symbol,
            subject,
            parse_status,
            event_status,
            concise_summary_text
        FROM advisory_watch_events
        ORDER BY published_on DESC, symbol
        LIMIT {int(limit)}
        """
    )
    if df.empty:
        return df
    for column in ["published_on", "asof_date"]:
        df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    return df


def load_rebalance_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 25) -> pd.DataFrame:
    clauses = ["1 = 1"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append(f"asof_date = (SELECT MAX(asof_date) FROM {REBALANCE_TABLE})")
    df = sql_to_df(
        f"""
        SELECT
            asof_date,
            published_on,
            setup_id,
            symbol,
            unique_id,
            suggested_action,
            action_reason,
            reference_price,
            stop_price,
            invalidation_price,
            recommended_stop_price,
            action_fraction,
            execution_mode,
            context_snapshot_json,
            load_ts
        FROM {REBALANCE_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC, symbol
        LIMIT {int(limit)}
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    for column in ["asof_date", "published_on", "load_ts"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    for column in ["reference_price", "stop_price", "invalidation_price", "recommended_stop_price", "action_fraction"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.sort_values(
        ["symbol", "published_on", "load_ts", "setup_id"],
        ascending=[True, False, False, True],
        kind="stable",
    ).drop_duplicates(subset=["symbol", "suggested_action"], keep="first").reset_index(drop=True)


def load_execution_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 100) -> pd.DataFrame:
    clauses = ["1 = 1"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append(f"asof_date = (SELECT MAX(asof_date) FROM {EXECUTION_TABLE})")
    df = sql_to_df(
        f"""
        SELECT
            asof_date,
            published_on,
            setup_id,
            symbol,
            unique_id,
            transaction_type,
            product_type,
            order_type,
            quantity,
            reference_price,
            execution_status,
            execution_reason,
            broker_order_status,
            submitted_at,
            broker_update_time,
            load_ts
        FROM {EXECUTION_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY COALESCE(submitted_at, load_ts, published_on) DESC, symbol
        LIMIT {int(limit)}
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    for column in ["asof_date", "published_on", "submitted_at", "broker_update_time", "load_ts"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    for column in ["quantity", "reference_price"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df.sort_values(
        ["symbol", "submitted_at", "load_ts", "published_on"],
        ascending=[True, False, False, False],
        kind="stable",
    ).drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)


def load_action_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 100) -> pd.DataFrame:
    if not _table_columns(ACTIONS_TABLE):
        try:
            df = build_action_recommendations(asof_date=asof_date)
        except Exception:
            return pd.DataFrame()
        if df.empty:
            return df
        return df.head(int(limit)).copy()
    clauses = ["1 = 1"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append(f"asof_date = (SELECT MAX(asof_date) FROM {ACTIONS_TABLE})")
    df = sql_to_df(
        f"""
        SELECT
            asof_date,
            published_on,
            symbol,
            setup_id,
            unique_id,
            action_code,
            action_priority,
            action_source,
            source_action,
            transaction_type,
            execution_mode,
            action_fraction,
            approved_allocation_inr,
            reference_price,
            stop_price,
            invalidation_price,
            recommended_stop_price,
            invest_score_pct,
            action_reason,
            action_detail,
            load_ts
        FROM {ACTIONS_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY action_priority DESC, published_on DESC, symbol
        LIMIT {int(limit)}
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    for column in ["asof_date", "published_on", "load_ts"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    for column in ["action_fraction", "approved_allocation_inr", "reference_price", "stop_price", "invalidation_price", "recommended_stop_price", "invest_score_pct"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df.sort_values(
        ["symbol", "action_priority", "published_on", "setup_id"],
        ascending=[True, False, False, True],
        kind="stable",
    ).drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)


def load_alert_rows(limit: int = 100) -> pd.DataFrame:
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {ALERTS_TABLE}
            ORDER BY observed_at DESC, symbol
            LIMIT {int(limit)}
            """
        )
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    for column in ["observed_at", "asof_date", "load_ts"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    return df


def load_latest_prices(symbols: list[str], *, asof_date: pd.Timestamp | None = None) -> pd.DataFrame:
    normalized = sorted({str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()})
    if not normalized:
        return pd.DataFrame(columns=["symbol", "price_asof_date", "adj_close", "price_source"])
    intraday_clauses = ["ticker = ANY(%s)"]
    intraday_params: list[object] = [normalized]
    daily_clauses = ["ticker = ANY(%s)"]
    daily_params: list[object] = [normalized]
    df = sql_to_df(
        f"""
        WITH intraday_latest AS (
            SELECT DISTINCT ON (ticker)
                ticker AS symbol,
                "timestamp" AS price_asof_date,
                close AS adj_close,
                'intraday' AS price_source
            FROM dhan_ohlcv_intraday
            WHERE {' AND '.join(intraday_clauses)}
            ORDER BY ticker, "timestamp" DESC, load_ts DESC
        ),
        daily_latest AS (
            SELECT DISTINCT ON (ticker)
                ticker AS symbol,
                date AS price_asof_date,
                close AS adj_close,
                'daily' AS price_source
            FROM dhan_ohlcv_daily
            WHERE {' AND '.join(daily_clauses)}
            ORDER BY ticker, date DESC, load_ts DESC
        )
        SELECT DISTINCT ON (symbol)
            symbol,
            price_asof_date,
            adj_close,
            price_source
        FROM (
            SELECT * FROM intraday_latest
            UNION ALL
            SELECT * FROM daily_latest
        ) prices
        ORDER BY symbol, CASE WHEN price_source = 'intraday' THEN 0 ELSE 1 END, price_asof_date DESC
        """,
        params=tuple(intraday_params + daily_params),
    )
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["price_asof_date"] = pd.to_datetime(df["price_asof_date"], utc=True, errors="coerce")
    df["adj_close"] = pd.to_numeric(df["adj_close"], errors="coerce")
    if "price_source" in df.columns:
        df["price_source"] = df["price_source"].astype("string")
    return df


def load_event_digest(
    table_name: str,
    *,
    symbols: list[str],
    asof_date: pd.Timestamp | None = None,
    days_back: int = 14,
    limit: int = 400,
) -> dict[tuple[str | None, str], list[dict[str, Any]]]:
    normalized = sorted({str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()})
    if not normalized:
        return {}
    clauses = ["symbol = ANY(%s)"]
    params: list[object] = [normalized]
    if asof_date is not None:
        published_from = pd.Timestamp(asof_date) - pd.Timedelta(days=int(days_back))
        clauses.append("published_on >= %s")
        clauses.append("published_on <= %s")
        params.extend([published_from, asof_date + pd.Timedelta(days=1)])
    df = sql_to_df(
        f"""
        SELECT
            published_on,
            asof_date,
            setup_id,
            symbol,
            subject,
            concise_summary_text,
            event_status
        FROM {table_name}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC
        LIMIT {int(limit)}
        """,
        params=tuple(params),
    )
    if df.empty:
        return {}
    for column in ["published_on", "asof_date"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    if "setup_id" in df.columns:
        df["setup_id"] = df["setup_id"].astype("string").str.upper()
    if "event_status" in df.columns:
        df = df[df["event_status"].fillna("triggered").astype("string").str.lower().eq("triggered")]
    digest: dict[tuple[str | None, str], list[dict[str, Any]]] = {}
    for _, row in df.iterrows():
        symbol = _as_text(row.get("symbol"))
        if not symbol:
            continue
        published_on = to_display_timestamp(row.get("published_on"))
        item = {
            "published_on": published_on,
            "subject": _as_text(row.get("subject")),
            "concise_summary_text": _as_text(row.get("concise_summary_text")),
        }
        setup_key = _as_text(row.get("setup_id"))
        digest.setdefault((setup_key, symbol), [])
        if len(digest[(setup_key, symbol)]) < 2:
            digest[(setup_key, symbol)].append(item)
        digest.setdefault((None, symbol), [])
        if len(digest[(None, symbol)]) < 2:
            digest[(None, symbol)].append(item)
    return digest


def load_price_history(
    symbols: list[str],
    *,
    intraday_days: int = 14,
    daily_days: int = 90,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    normalized = sorted({str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()})
    if not normalized:
        return pd.DataFrame(), pd.DataFrame()
    intraday_from = pd.Timestamp.utcnow() - pd.Timedelta(days=int(intraday_days))
    daily_from = pd.Timestamp.utcnow() - pd.Timedelta(days=int(daily_days))
    intraday = sql_to_df(
        """
        SELECT
            ticker AS symbol,
            "timestamp" AS price_ts,
            close
        FROM dhan_ohlcv_intraday
        WHERE ticker = ANY(%s)
          AND "timestamp" >= %s
        ORDER BY ticker, "timestamp"
        """,
        params=(normalized, intraday_from),
    )
    daily = sql_to_df(
        """
        SELECT
            ticker AS symbol,
            date AS price_ts,
            close
        FROM dhan_ohlcv_daily
        WHERE ticker = ANY(%s)
          AND date >= %s
        ORDER BY ticker, date
        """,
        params=(normalized, daily_from),
    )
    for frame in [intraday, daily]:
        if not frame.empty:
            frame["symbol"] = frame["symbol"].astype("string").str.upper()
            frame["price_ts"] = pd.to_datetime(frame["price_ts"], utc=True, errors="coerce")
            frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    return intraday, daily


def resolve_entry_price_from_history(
    symbol: str,
    anchor_ts: Any,
    *,
    intraday_history: pd.DataFrame,
    daily_history: pd.DataFrame,
) -> float | None:
    anchor = pd.to_datetime(anchor_ts, utc=True, errors="coerce")
    if pd.isna(anchor):
        return None
    intraday_slice = intraday_history[
        intraday_history["symbol"].eq(symbol.upper())
        & intraday_history["price_ts"].le(anchor)
    ] if not intraday_history.empty else pd.DataFrame()
    if not intraday_slice.empty:
        value = pd.to_numeric(intraday_slice.iloc[-1].get("close"), errors="coerce")
        return None if pd.isna(value) else float(value)
    daily_slice = daily_history[
        daily_history["symbol"].eq(symbol.upper())
        & daily_history["price_ts"].le(anchor)
    ] if not daily_history.empty else pd.DataFrame()
    if not daily_slice.empty:
        value = pd.to_numeric(daily_slice.iloc[-1].get("close"), errors="coerce")
        return None if pd.isna(value) else float(value)
    return None


def load_operator_feed(*, output_dir: str | Path = DEFAULT_OUTPUT_DIR, limit: int = 50) -> list[dict[str, Any]]:
    feed_path = Path(output_dir) / "operator_feed.json"
    if not feed_path.exists():
        return []
    try:
        payload = json.loads(feed_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    return payload[-int(limit) :]


def load_runtime_processes(limit: int = 30) -> list[dict[str, Any]]:
    try:
        output = subprocess.check_output(
            [
                "ps",
                "-eo",
                "pid,etimes,args",
            ],
            text=True,
        )
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in output.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        pid, etimes, args = parts
        if "stockey" not in args and "advisory." not in args and "data." not in args and "go-crond" not in args:
            continue
        rows.append(
            {
                "pid": int(pid),
                "elapsed_seconds": int(etimes),
                "command": args,
            }
        )
    rows.sort(key=lambda item: item["elapsed_seconds"], reverse=True)
    return rows[: int(limit)]


def load_cron_status(*, log_dir: str | Path = DEFAULT_CRON_LOG_DIR, tail_lines: int = 8) -> list[dict[str, Any]]:
    log_path = Path(log_dir)
    if not log_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for file_path in sorted(log_path.glob("*.log")):
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            content = []
        stat = file_path.stat()
        rows.append(
            {
                "log_file": file_path.name,
                "modified_at": to_display_timestamp(pd.Timestamp(stat.st_mtime, unit="s", tz="UTC")),
                "size_bytes": int(stat.st_size),
                "tail": "\n".join(content[-int(tail_lines):]) if content else "",
            }
        )
    rows.sort(key=lambda item: item["modified_at"], reverse=True)
    return rows


def _build_aux_maps(
    *,
    asof_date: pd.Timestamp | None,
    portfolio_df: pd.DataFrame,
    lifecycle_df: pd.DataFrame,
    watchlist_df: pd.DataFrame,
    rebalance_df: pd.DataFrame,
) -> dict[str, Any]:
    symbol_pool: set[str] = set()
    for frame in [portfolio_df, lifecycle_df, watchlist_df, rebalance_df]:
        if not frame.empty and "symbol" in frame.columns:
            symbol_pool.update(frame["symbol"].dropna().astype(str).str.upper().tolist())
    price_df = _safe_frame_loader(load_latest_prices, sorted(symbol_pool), asof_date=asof_date)
    execution_df = _safe_frame_loader(load_execution_rows, asof_date=asof_date, limit=max(len(symbol_pool) * 3, 100))
    action_df = _safe_frame_loader(load_action_rows, asof_date=asof_date, limit=max(len(symbol_pool) * 3, 100))
    price_map = {
        str(row["symbol"]).upper(): {
            "current_price": _as_float(row.get("adj_close")),
            "price_asof_date": row.get("price_asof_date"),
        }
        for _, row in price_df.iterrows()
    } if not price_df.empty else {}
    execution_map = {
        str(row["symbol"]).upper(): row.to_dict()
        for _, row in execution_df.iterrows()
    } if not execution_df.empty else {}
    action_map = {
        str(row["symbol"]).upper(): row.to_dict()
        for _, row in action_df.iterrows()
    } if not action_df.empty else {}
    news_map = _safe_list_loader(lambda: load_event_digest("advisory_news_events", symbols=sorted(symbol_pool), asof_date=asof_date))
    announcement_map = _safe_list_loader(lambda: load_event_digest("advisory_watch_events", symbols=sorted(symbol_pool), asof_date=asof_date))
    portfolio_map = {
        (str(row.get("setup_id") or "").upper(), str(row.get("symbol") or "").upper()): row.to_dict()
        for _, row in portfolio_df.iterrows()
    } if not portfolio_df.empty else {}
    lifecycle_map = {
        (str(row.get("setup_id") or "").upper(), str(row.get("symbol") or "").upper()): row.to_dict()
        for _, row in lifecycle_df.iterrows()
    } if not lifecycle_df.empty else {}
    return {
        "price_map": price_map,
        "action_map": action_map,
        "execution_map": execution_map,
        "news_map": news_map if isinstance(news_map, dict) else {},
        "announcement_map": announcement_map if isinstance(announcement_map, dict) else {},
        "portfolio_map": portfolio_map,
        "lifecycle_map": lifecycle_map,
    }


def _event_summary(event_map: dict[tuple[str | None, str], list[dict[str, Any]]], setup_id: Any, symbol: Any) -> str | None:
    symbol_key = _as_text(symbol)
    if not symbol_key:
        return None
    setup_key = _as_text(setup_id)
    rows = event_map.get((setup_key.upper() if setup_key else None, symbol_key.upper())) or event_map.get((None, symbol_key.upper())) or []
    return _format_summary_lines(rows)


def _base_recommendation_record(
    *,
    kind: str,
    setup_id: Any,
    setup_name: Any = None,
    setup_family: Any = None,
    symbol: Any,
    entry_date: Any = None,
    entry_price: Any = None,
    current_price: Any = None,
    pnl_pct: Any = None,
    reason: Any = None,
    reason_detail: Any = None,
    exit_strategy: Any = None,
    news_summary: Any = None,
    announcement_summary: Any = None,
    status: Any = None,
    allocation_inr: Any = None,
    current_value_note: Any = None,
    market_context: Any = None,
    screener_context: Any = None,
    setup_context: Any = None,
    technical_context: Any = None,
    action_summary: Any = None,
    execution_intent: Any = None,
    sort_ts: Any = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "setup_id": _as_text(setup_id),
        "setup_name": _as_text(setup_name),
        "setup_family": _as_text(setup_family),
        "symbol": _as_text(symbol),
        "entry_date": _display_dashboard_timestamp(entry_date, prefer_date_for_midnight_utc=True),
        "entry_price": _as_float(entry_price),
        "current_price": _as_float(current_price),
        "pnl_pct": _as_float(pnl_pct),
        "invest_score_pct": None,
        "reason": _as_text(reason),
        "reason_detail": _as_text(reason_detail),
        "exit_strategy": _as_text(exit_strategy),
        "news_summary": _as_text(news_summary),
        "announcement_summary": _as_text(announcement_summary),
        "status": _as_text(status),
        "allocation_inr": _as_float(allocation_inr),
        "current_value_note": _as_text(current_value_note),
        "market_context": _as_text(market_context),
        "screener_context": _as_text(screener_context),
        "setup_context": _as_text(setup_context),
        "technical_context": _as_text(technical_context),
        "action_summary": _as_text(action_summary),
        "execution_intent": _as_text(execution_intent),
        "sort_ts": None if pd.isna(pd.to_datetime(sort_ts, utc=True, errors="coerce")) else pd.to_datetime(sort_ts, utc=True, errors="coerce").isoformat(),
    }


def _coalesce_float(*values: Any) -> float | None:
    for value in values:
        numeric = _as_float(value)
        if numeric is not None:
            return numeric
    return None


def _sort_recommendation_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _sort_key(row: dict[str, Any]) -> tuple[pd.Timestamp, str, str]:
        entry_ts = pd.to_datetime(row.get("sort_ts") or row.get("entry_date"), utc=True, errors="coerce")
        if pd.isna(entry_ts):
            entry_ts = pd.Timestamp.min.tz_localize("UTC")
        return (entry_ts, str(row.get("setup_id") or ""), str(row.get("symbol") or ""))

    return sorted(rows, key=_sort_key, reverse=True)


def _sort_action_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority_order = {
        "SELL": 100,
        "PARTIAL_SELL": 90,
        "MANUAL_REVIEW": 80,
        "TIGHTEN_STOP": 70,
        "BUY_MORE": 60,
        "BUY": 50,
        "WATCH": 10,
        "HOLD": 0,
    }

    def _sort_key(row: dict[str, Any]) -> tuple[int, pd.Timestamp, str, str]:
        action = str(row.get("status") or "").upper()
        action_priority = int(priority_order.get(action, 0))
        entry_ts = pd.to_datetime(row.get("sort_ts") or row.get("entry_date"), utc=True, errors="coerce")
        if pd.isna(entry_ts):
            entry_ts = pd.Timestamp.min.tz_localize("UTC")
        return (action_priority, entry_ts, str(row.get("setup_id") or ""), str(row.get("symbol") or ""))

    return sorted(rows, key=_sort_key, reverse=True)


def build_recommendation_views(
    *,
    asof_date: pd.Timestamp | None,
    dashboard_df: pd.DataFrame,
    portfolio_df: pd.DataFrame,
    candidates_df: pd.DataFrame,
    lifecycle_df: pd.DataFrame,
    watchlist_df: pd.DataFrame,
    rebalance_df: pd.DataFrame,
) -> dict[str, list[dict[str, Any]]]:
    aux = _build_aux_maps(
        asof_date=asof_date,
        portfolio_df=portfolio_df,
        lifecycle_df=lifecycle_df,
        watchlist_df=watchlist_df,
        rebalance_df=rebalance_df,
    )
    price_map = aux["price_map"]
    action_map = aux["action_map"]
    execution_map = aux["execution_map"]
    news_map = aux["news_map"]
    announcement_map = aux["announcement_map"]
    portfolio_map = aux["portfolio_map"]
    lifecycle_map = aux["lifecycle_map"]
    candidate_map = {
        (str(row.get("setup_id") or "").upper(), str(row.get("symbol") or "").upper()): row.to_dict()
        for _, row in candidates_df.iterrows()
    } if not candidates_df.empty else {}
    setup_meta_map = _setup_metadata()
    setup_dashboard_map = _setup_dashboard_map(dashboard_df)
    symbol_pool = sorted(
        {
            str(symbol).upper().strip()
            for symbol in (
                list(portfolio_df.get("symbol", pd.Series(dtype="object")))
                + list(lifecycle_df.get("symbol", pd.Series(dtype="object")))
                + list(watchlist_df.get("symbol", pd.Series(dtype="object")))
                + list(rebalance_df.get("symbol", pd.Series(dtype="object")))
            )
            if str(symbol).strip()
        }
    )
    intraday_history, daily_history = load_price_history(symbol_pool)

    today_rows: list[dict[str, Any]] = []
    seen_today: set[tuple[str, str]] = set()
    today_anchor = None
    if not portfolio_df.empty and "published_on" in portfolio_df.columns:
        published_series = pd.to_datetime(portfolio_df["published_on"], utc=True, errors="coerce").dropna()
        if not published_series.empty:
            today_anchor = published_series.max().normalize()
    if not portfolio_df.empty:
        for _, row in portfolio_df.sort_values(["published_on", "symbol"], ascending=[False, True]).iterrows():
            setup_id = _as_text(row.get("setup_id"))
            symbol = _as_text(row.get("symbol"))
            if not setup_id or not symbol:
                continue
            setup_snapshot = setup_dashboard_map.get(setup_id.upper(), {})
            setup_meta = setup_meta_map.get(setup_id.upper(), {})
            key = (setup_id.upper(), symbol.upper())
            candidate_row = candidate_map.get(key, {})
            if key in seen_today:
                continue
            published_on = pd.to_datetime(row.get("published_on"), utc=True, errors="coerce")
            if today_anchor is not None and (pd.isna(published_on) or published_on.normalize() != today_anchor):
                continue
            seen_today.add(key)
            context = _parse_json_blob(row.get("context_snapshot_json"))
            price_row = price_map.get(symbol.upper(), {})
            action_row = action_map.get(symbol.upper(), {})
            entry_price = resolve_entry_price_from_history(
                symbol,
                row.get("planned_at") or row.get("published_on"),
                intraday_history=intraday_history,
                daily_history=daily_history,
            )
            if entry_price is None:
                entry_price = _as_float(context.get("adj_close"))
            if entry_price is None:
                entry_price = _as_float(price_row.get("current_price"))
            current_price = _as_float(price_row.get("current_price"))
            if current_price is None:
                current_price = entry_price
            pnl_pct = None
            if entry_price is not None and current_price is not None and entry_price > 0:
                pnl_pct = round(((current_price / entry_price) - 1.0) * 100.0, 4)
            today_rows.append(
                _base_recommendation_record(
                    kind="today",
                    setup_id=setup_id,
                    setup_name=setup_meta.get("setup_name"),
                    setup_family=setup_meta.get("setup_family"),
                    symbol=symbol,
                    entry_date=row.get("published_on"),
                    entry_price=entry_price,
                    current_price=current_price,
                    pnl_pct=pnl_pct,
                    reason=_recommendation_reason(row),
                    reason_detail=_merge_reason_detail(
                        _recommendation_detail(row),
                        _technical_state_summary(candidate_row),
                        _technical_interpretation(candidate_row),
                        _technical_score_breakdown(candidate_row),
                    ),
                    exit_strategy=_format_exit_strategy(row),
                    news_summary=_event_summary(news_map, setup_id, symbol),
                    announcement_summary=_event_summary(announcement_map, setup_id, symbol),
                    status=action_row.get("action_code") or row.get("portfolio_status"),
                    allocation_inr=action_row.get("approved_allocation_inr") if action_row else row.get("approved_allocation_inr"),
                    current_value_note=row.get("execution_notes"),
                    market_context=_market_context_text(setup_snapshot),
                    screener_context=_screener_context_text(setup_snapshot, setup_meta),
                    setup_context=_setup_context_text(setup_snapshot, setup_meta),
                    technical_context=_merge_reason_detail(
                        _technical_state_summary(candidate_row),
                        _technical_interpretation(candidate_row),
                        _technical_score_breakdown(candidate_row),
                        None,
                    ),
                    action_summary=_format_action_summary(action_row) if action_row else None,
                    execution_intent=_format_execution_intent(execution_map.get(symbol.upper(), {})),
                    sort_ts=row.get("published_on"),
                )
            )
            today_rows[-1]["reason"] = _as_text(action_row.get("action_code")) or today_rows[-1]["reason"]
            today_rows[-1]["reason_detail"] = _merge_reason_detail(
                _as_text(action_row.get("action_reason")),
                _as_text(action_row.get("action_detail")),
                today_rows[-1]["reason_detail"],
            )
            today_rows[-1]["invest_score_pct"] = _coalesce_float(action_row.get("invest_score_pct"), row.get("invest_score_pct"))

    current_rows: list[dict[str, Any]] = []
    seen_current: set[tuple[str, str]] = set()
    if not lifecycle_df.empty:
        for _, row in lifecycle_df.sort_values(["published_on", "symbol"], ascending=[False, True]).iterrows():
            setup_id = _as_text(row.get("setup_id"))
            symbol = _as_text(row.get("symbol"))
            if not setup_id or not symbol:
                continue
            setup_snapshot = setup_dashboard_map.get(setup_id.upper(), {})
            setup_meta = setup_meta_map.get(setup_id.upper(), {})
            key = (setup_id.upper(), symbol.upper())
            if key in seen_current:
                continue
            seen_current.add(key)
            portfolio_row = portfolio_map.get((setup_id.upper(), symbol.upper()), {})
            price_row = price_map.get(symbol.upper(), {})
            action_row = action_map.get(symbol.upper(), {})
            context = _parse_json_blob(row.get("context_snapshot_json") or portfolio_row.get("context_snapshot_json"))
            current_price = _as_float(price_row.get("current_price"))
            if current_price is None:
                current_price = _as_float(row.get("current_price"))
            assumed_entry = _as_float(row.get("entry_price"))
            if assumed_entry is None:
                assumed_entry = resolve_entry_price_from_history(
                    symbol,
                    row.get("entry_date") or row.get("published_on"),
                    intraday_history=intraday_history,
                    daily_history=daily_history,
                )
            if assumed_entry is None:
                assumed_entry = _as_float(context.get("entry_price"))
            if assumed_entry is None:
                assumed_entry = _as_float(context.get("adj_close"))
            if assumed_entry is None:
                assumed_entry = current_price
            pnl_pct = None
            if assumed_entry is not None and current_price is not None and assumed_entry > 0:
                pnl_pct = round(((current_price / assumed_entry) - 1.0) * 100.0, 4)
            current_rows.append(
                _base_recommendation_record(
                    kind="current",
                    setup_id=setup_id,
                    setup_name=setup_meta.get("setup_name"),
                    setup_family=setup_meta.get("setup_family"),
                    symbol=symbol,
                    entry_date=row.get("entry_date") or row.get("published_on"),
                    entry_price=assumed_entry,
                    current_price=current_price,
                    pnl_pct=pnl_pct,
                    reason=_recommendation_reason(row),
                    reason_detail=_merge_reason_detail(
                        _recommendation_detail({**portfolio_row, **row.to_dict()}),
                        _technical_state_summary(candidate_map.get((setup_id.upper(), symbol.upper()), {})),
                        _technical_interpretation(candidate_map.get((setup_id.upper(), symbol.upper()), {})),
                        _technical_score_breakdown(candidate_map.get((setup_id.upper(), symbol.upper()), {})),
                    ),
                    exit_strategy=_format_exit_strategy({**portfolio_row, **row.to_dict()}),
                    news_summary=_event_summary(news_map, setup_id, symbol),
                    announcement_summary=_event_summary(announcement_map, setup_id, symbol),
                    status=action_row.get("action_code") or row.get("position_status"),
                    allocation_inr=(action_row.get("approved_allocation_inr") if action_row else None) or row.get("approved_allocation_inr") or portfolio_row.get("approved_allocation_inr"),
                    current_value_note=row.get("next_action_reason"),
                    market_context=_market_context_text(setup_snapshot),
                    screener_context=_screener_context_text(setup_snapshot, setup_meta),
                    setup_context=_setup_context_text(setup_snapshot, setup_meta),
                    technical_context=_merge_reason_detail(
                        _technical_state_summary(candidate_map.get((setup_id.upper(), symbol.upper()), {})),
                        _technical_interpretation(candidate_map.get((setup_id.upper(), symbol.upper()), {})),
                        _technical_score_breakdown(candidate_map.get((setup_id.upper(), symbol.upper()), {})),
                        None,
                    ),
                    action_summary=_format_action_summary(action_row) if action_row else None,
                    execution_intent=_format_execution_intent(execution_map.get(symbol.upper(), {})),
                    sort_ts=row.get("published_on"),
                )
            )
            current_rows[-1]["reason"] = _as_text(action_row.get("action_code")) or current_rows[-1]["reason"]
            current_rows[-1]["reason_detail"] = _merge_reason_detail(
                _as_text(action_row.get("action_reason")),
                _as_text(action_row.get("action_detail")),
                current_rows[-1]["reason_detail"],
            )
            current_rows[-1]["invest_score_pct"] = _coalesce_float(action_row.get("invest_score_pct"), row.get("invest_score_pct"), portfolio_row.get("invest_score_pct"))

    watch_rows: list[dict[str, Any]] = []
    seen_watch: set[tuple[str, str]] = set()
    if not watchlist_df.empty:
        for _, row in watchlist_df.sort_values(["state_updated_at", "symbol"], ascending=[False, True]).iterrows():
            setup_id = _as_text(row.get("setup_id"))
            symbol = _as_text(row.get("symbol"))
            if not setup_id or not symbol:
                continue
            setup_snapshot = setup_dashboard_map.get(setup_id.upper(), {})
            setup_meta = setup_meta_map.get(setup_id.upper(), {})
            key = (setup_id.upper(), symbol.upper())
            if key in seen_watch:
                continue
            status_text = " ".join(
                [
                    _as_text(row.get("watch_status")) or "",
                    _as_text(row.get("current_state")) or "",
                    _as_text(row.get("candidate_state")) or "",
                ]
            ).lower()
            if "abstain" in status_text:
                continue
            seen_watch.add(key)
            candidate_row = candidate_map.get(key, {})
            price_row = price_map.get(symbol.upper(), {})
            action_row = action_map.get(symbol.upper(), {})
            current_price = _as_float(price_row.get("current_price"))
            low = _as_float(row.get("attractive_price_low"))
            high = _as_float(row.get("attractive_price_high"))
            gap_note = None
            if current_price is not None and low is not None and high is not None:
                if current_price < low:
                    gap_note = f"Below preferred entry zone by {round(((low / current_price) - 1.0) * 100.0, 2)}%."
                elif current_price > high:
                    gap_note = f"Above preferred entry zone by {round(((current_price / high) - 1.0) * 100.0, 2)}%."
                else:
                    gap_note = "Inside preferred entry zone."
            watch_rows.append(
                _base_recommendation_record(
                    kind="watch",
                    setup_id=setup_id,
                    setup_name=setup_meta.get("setup_name"),
                    setup_family=setup_meta.get("setup_family"),
                    symbol=symbol,
                    entry_date=row.get("state_updated_at"),
                    entry_price=low,
                    current_price=current_price,
                    pnl_pct=None,
                    reason=_recommendation_reason(row),
                    reason_detail=_merge_reason_detail(
                        _recommendation_detail(row),
                        _technical_state_summary(candidate_row),
                        _technical_interpretation(candidate_row),
                        _technical_score_breakdown(candidate_row),
                    ),
                    exit_strategy=_format_exit_strategy(row),
                    news_summary=_event_summary(news_map, setup_id, symbol),
                    announcement_summary=_event_summary(announcement_map, setup_id, symbol),
                    status=action_row.get("action_code") or row.get("watch_status") or row.get("current_state"),
                    allocation_inr=None,
                    current_value_note=gap_note,
                    market_context=_market_context_text(setup_snapshot),
                    screener_context=_screener_context_text(setup_snapshot, setup_meta),
                    setup_context=_setup_context_text(setup_snapshot, setup_meta),
                    technical_context=_merge_reason_detail(
                        _technical_state_summary(candidate_row),
                        _technical_interpretation(candidate_row),
                        _technical_score_breakdown(candidate_row),
                        None,
                    ),
                    action_summary=_format_action_summary(action_row) if action_row else None,
                    execution_intent=_format_execution_intent(execution_map.get(symbol.upper(), {})),
                    sort_ts=row.get("state_updated_at"),
                )
            )
            setup_score = _as_float(row.get("setup_score"))
            watch_rows[-1]["reason"] = _as_text(action_row.get("action_code")) or watch_rows[-1]["reason"]
            watch_rows[-1]["reason_detail"] = _merge_reason_detail(
                _as_text(action_row.get("action_reason")),
                _as_text(action_row.get("action_detail")),
                watch_rows[-1]["reason_detail"],
            )
            watch_rows[-1]["invest_score_pct"] = _coalesce_float(
                action_row.get("invest_score_pct"),
                row.get("invest_score_pct"),
                (setup_score * 100.0) if setup_score is not None else None,
            )

    top_action_rows: list[dict[str, Any]] = []
    if action_map:
        display_candidates: dict[str, dict[str, Any]] = {}
        for row in current_rows + watch_rows + today_rows:
            symbol = _as_text(row.get("symbol"))
            if symbol and symbol.upper() not in display_candidates:
                display_candidates[symbol.upper()] = row
        for symbol_key, action_row in action_map.items():
            action_code = _as_text(action_row.get("action_code"))
            if action_code in {None, "HOLD", "WATCH"}:
                continue
            base_row = display_candidates.get(symbol_key, {})
            setup_id = _as_text(action_row.get("setup_id")) or _as_text(base_row.get("setup_id"))
            setup_snapshot = setup_dashboard_map.get((setup_id or "").upper(), {})
            setup_meta = setup_meta_map.get((setup_id or "").upper(), {})
            top_action_rows.append(
                _base_recommendation_record(
                    kind="action_top",
                    setup_id=setup_id,
                    setup_name=_as_text(base_row.get("setup_name")) or setup_meta.get("setup_name"),
                    setup_family=_as_text(base_row.get("setup_family")) or setup_meta.get("setup_family"),
                    symbol=symbol_key,
                    entry_date=action_row.get("published_on") or base_row.get("entry_date"),
                    entry_price=base_row.get("entry_price"),
                    current_price=base_row.get("current_price") or price_map.get(symbol_key, {}).get("current_price"),
                    pnl_pct=base_row.get("pnl_pct"),
                    reason=action_code,
                    reason_detail=_merge_reason_detail(action_row.get("action_reason"), action_row.get("action_detail"), base_row.get("reason_detail")),
                    exit_strategy=_format_exit_strategy(action_row),
                    news_summary=_event_summary(news_map, setup_id, symbol_key),
                    announcement_summary=_event_summary(announcement_map, setup_id, symbol_key),
                    status=action_code,
                    allocation_inr=action_row.get("approved_allocation_inr") or base_row.get("allocation_inr"),
                    current_value_note=base_row.get("current_value_note"),
                    market_context=base_row.get("market_context") or _market_context_text(setup_snapshot),
                    screener_context=base_row.get("screener_context") or _screener_context_text(setup_snapshot, setup_meta),
                    setup_context=base_row.get("setup_context") or _setup_context_text(setup_snapshot, setup_meta),
                    technical_context=base_row.get("technical_context"),
                    action_summary=_format_action_summary(action_row),
                    execution_intent=_format_execution_intent(execution_map.get(symbol_key, {})),
                    sort_ts=action_row.get("published_on"),
                )
            )
            top_action_rows[-1]["invest_score_pct"] = _coalesce_float(action_row.get("invest_score_pct"), base_row.get("invest_score_pct"))

    exited_rows: list[dict[str, Any]] = []
    action_rows: list[dict[str, Any]] = []
    seen_exited: set[tuple[str, str, str]] = set()
    if not rebalance_df.empty:
        for _, row in rebalance_df.sort_values(["published_on", "symbol"], ascending=[False, True]).iterrows():
            setup_id = _as_text(row.get("setup_id"))
            symbol = _as_text(row.get("symbol"))
            if not setup_id or not symbol:
                continue
            setup_snapshot = setup_dashboard_map.get(setup_id.upper(), {})
            setup_meta = setup_meta_map.get(setup_id.upper(), {})
            action_key = _as_text(row.get("suggested_action")) or "-"
            key = (setup_id.upper(), symbol.upper(), action_key.upper())
            if key in seen_exited:
                continue
            seen_exited.add(key)
            lifecycle_row = lifecycle_map.get((setup_id.upper(), symbol.upper()), {})
            context = _parse_json_blob(row.get("context_snapshot_json"))
            current_price = _as_float(row.get("reference_price"))
            live_price = _as_float(price_map.get(symbol.upper(), {}).get("current_price"))
            if live_price is not None:
                current_price = live_price
            entry_price = _as_float(lifecycle_row.get("entry_price"))
            if entry_price is None:
                entry_price = resolve_entry_price_from_history(
                    symbol,
                    lifecycle_row.get("entry_date") or lifecycle_row.get("published_on"),
                    intraday_history=intraday_history,
                    daily_history=daily_history,
                )
            if entry_price is None:
                entry_price = _as_float(context.get("entry_price"))
            pnl_pct = None
            if current_price is not None and entry_price is not None and entry_price > 0:
                pnl_pct = round(((current_price / entry_price) - 1.0) * 100.0, 4)
            record = _base_recommendation_record(
                kind="exited",
                setup_id=setup_id,
                setup_name=setup_meta.get("setup_name"),
                setup_family=setup_meta.get("setup_family"),
                symbol=symbol,
                entry_date=lifecycle_row.get("entry_date") or lifecycle_row.get("published_on"),
                entry_price=entry_price,
                current_price=current_price,
                pnl_pct=pnl_pct,
                reason=row.get("suggested_action"),
                reason_detail=row.get("action_reason"),
                exit_strategy=_format_exit_strategy(row),
                news_summary=_event_summary(news_map, setup_id, symbol),
                announcement_summary=_event_summary(announcement_map, setup_id, symbol),
                status=row.get("suggested_action"),
                allocation_inr=lifecycle_row.get("approved_allocation_inr"),
                current_value_note=None,
                market_context=_market_context_text(setup_snapshot),
                screener_context=_screener_context_text(setup_snapshot, setup_meta),
                setup_context=_setup_context_text(setup_snapshot, setup_meta),
                action_summary=_format_action_summary(row),
                execution_intent=_format_execution_intent(execution_map.get(symbol.upper(), {})),
                sort_ts=row.get("published_on"),
            )
            record["invest_score_pct"] = _coalesce_float(lifecycle_row.get("invest_score_pct"), row.get("invest_score_pct"))
            action_type = (_as_text(row.get("suggested_action")) or "").lower()
            published_on = pd.to_datetime(row.get("published_on"), utc=True, errors="coerce")
            if today_anchor is not None and not pd.isna(published_on) and published_on.normalize() == today_anchor:
                today_record = dict(record)
                today_record["kind"] = "today"
                today_record["status"] = row.get("suggested_action")
                today_record["reason"] = row.get("suggested_action")
                today_rows.append(today_record)
            if action_type.startswith("exit_"):
                exited_rows.append(record)
            else:
                record["kind"] = "action"
                action_rows.append(record)

    return {
        "top_action_recommendations": _sort_action_rows(top_action_rows),
        "today_recommendations": _sort_recommendation_rows(today_rows),
        "current_recommendations": _sort_recommendation_rows(current_rows),
        "watch_recommendations": _sort_recommendation_rows(watch_rows),
        "exited_recommendations": _sort_recommendation_rows(exited_rows),
        "action_recommendations": _sort_recommendation_rows(action_rows),
    }


def build_summary_snapshot(
    *,
    dashboard_df: pd.DataFrame,
    top_action_recommendations: list[dict[str, Any]],
    today_recommendations: list[dict[str, Any]],
    current_recommendations: list[dict[str, Any]],
    watch_recommendations: list[dict[str, Any]],
    exited_recommendations: list[dict[str, Any]],
    action_recommendations: list[dict[str, Any]],
    alerts_df: pd.DataFrame,
) -> dict[str, Any]:
    regime_names = []
    overlay_names = []
    overlay_reasons = []
    if not dashboard_df.empty:
        if "regime_name" in dashboard_df.columns:
            regime_names = [str(v) for v in dashboard_df["regime_name"].dropna().astype(str).unique().tolist() if v]
        if "overlay_name" in dashboard_df.columns:
            overlay_names = [str(v) for v in dashboard_df["overlay_name"].dropna().astype(str).unique().tolist() if v]
        if "overlay_reason" in dashboard_df.columns:
            overlay_reasons = [str(v) for v in dashboard_df["overlay_reason"].dropna().astype(str).unique().tolist() if v]
    open_count = sum(1 for row in current_recommendations if str(row.get("status") or "").lower() == "open")
    pending_count = sum(1 for row in current_recommendations if str(row.get("status") or "").lower() == "pending_entry")
    positive = sum(1 for row in current_recommendations if (_as_float(row.get("pnl_pct")) or 0.0) > 0)
    return {
        "regimes": regime_names,
        "overlays": overlay_names,
        "overlay_reasons": overlay_reasons[:3],
        "today_count": len(today_recommendations),
        "top_action_count": len(top_action_recommendations),
        "current_count": len(current_recommendations),
        "open_count": open_count,
        "pending_count": pending_count,
        "positive_count": positive,
        "watch_count": len(watch_recommendations),
        "exited_count": len(exited_recommendations),
        "action_count": len(action_recommendations),
        "alert_count": int(len(alerts_df)),
    }


def build_live_dashboard_payload(*, asof_date: pd.Timestamp | None = None, output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    dashboard_df = _safe_frame_loader(build_dashboard, asof_date=asof_date)
    portfolio_df = _safe_frame_loader(load_portfolio_rows, asof_date=asof_date)
    watchlist_df = _safe_frame_loader(load_watchlist_rows, asof_date=asof_date)
    candidates_df = _safe_frame_loader(load_candidate_rows, asof_date=asof_date)
    lifecycle_df = _safe_frame_loader(load_lifecycle_rows, asof_date=asof_date)
    rebalance_df = _safe_frame_loader(load_rebalance_rows, asof_date=asof_date)
    watch_events_df = _safe_frame_loader(load_recent_watch_events)
    alerts_df = _safe_frame_loader(load_alert_rows)
    sync_state_df = _safe_frame_loader(load_sync_states)
    operator_feed = _safe_list_loader(load_operator_feed, output_dir=output_dir)
    runtime_processes = _safe_list_loader(load_runtime_processes)
    cron_status = _safe_list_loader(load_cron_status)
    recommendation_views = build_recommendation_views(
        asof_date=asof_date,
        dashboard_df=dashboard_df,
        portfolio_df=portfolio_df,
        candidates_df=candidates_df,
        lifecycle_df=lifecycle_df,
        watchlist_df=watchlist_df,
        rebalance_df=rebalance_df,
    )
    summary = build_summary_snapshot(
        dashboard_df=dashboard_df,
        top_action_recommendations=recommendation_views["top_action_recommendations"],
        today_recommendations=recommendation_views["today_recommendations"],
        current_recommendations=recommendation_views["current_recommendations"],
        watch_recommendations=recommendation_views["watch_recommendations"],
        exited_recommendations=recommendation_views["exited_recommendations"],
        action_recommendations=recommendation_views["action_recommendations"],
        alerts_df=alerts_df,
    )
    return {
        "generated_at": to_display_timestamp(pd.Timestamp.utcnow()),
        "asof_date": None if asof_date is None else _display_dashboard_timestamp(asof_date, prefer_date_for_midnight_utc=True),
        "summary": _json_ready(summary),
        "top_action_recommendations": _json_ready(recommendation_views["top_action_recommendations"]),
        "today_recommendations": _json_ready(recommendation_views["today_recommendations"]),
        "current_recommendations": _json_ready(recommendation_views["current_recommendations"]),
        "watch_recommendations": _json_ready(recommendation_views["watch_recommendations"]),
        "exited_recommendations": _json_ready(recommendation_views["exited_recommendations"]),
        "action_recommendations": _json_ready(recommendation_views["action_recommendations"]),
        "dashboard": _json_ready(dashboard_df),
        "portfolio": _json_ready(portfolio_df),
        "watchlist": _json_ready(watchlist_df),
        "candidates": _json_ready(candidates_df),
        "lifecycle": _json_ready(lifecycle_df),
        "rebalance": _json_ready(rebalance_df),
        "watch_events": _json_ready(watch_events_df),
        "alerts": _json_ready(alerts_df),
        "operator_feed": _json_ready(operator_feed),
        "sync_state": _json_ready(sync_state_df),
        "runtime_processes": _json_ready(runtime_processes),
        "cron_status": _json_ready(cron_status),
    }


def render_html(payload: dict[str, Any]) -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stockey Live Dashboard</title>
  <style>
    :root {
      --bg: #f6f1e7;
      --surface: rgba(255, 250, 244, 0.78);
      --surface-strong: rgba(255, 255, 255, 0.9);
      --ink: #17211d;
      --muted: #61706a;
      --line: rgba(23, 33, 29, 0.12);
      --accent: #0f4c5c;
      --good: #0f7b47;
      --good-soft: rgba(15, 123, 71, 0.11);
      --warn: #9a5d00;
      --warn-soft: rgba(154, 93, 0, 0.12);
      --bad: #a12a2a;
      --bad-soft: rgba(161, 42, 42, 0.11);
      --shadow: 0 20px 60px rgba(31, 32, 24, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(15, 76, 92, 0.16), transparent 28%),
        radial-gradient(circle at bottom right, rgba(15, 123, 71, 0.12), transparent 24%),
        linear-gradient(180deg, rgba(255,255,255,0.42), rgba(255,255,255,0)),
        var(--bg);
      min-height: 100vh;
    }
    .wrap {
      max-width: 1440px;
      margin: 0 auto;
      padding: 24px;
    }
    h1, h2, h3 {
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", serif;
      font-weight: 600;
    }
    .hero {
      display: grid;
      grid-template-columns: 1.3fr 1fr;
      gap: 20px;
      align-items: end;
      padding: 28px;
      border-radius: 28px;
      background:
        linear-gradient(135deg, rgba(255,255,255,0.72), rgba(255,255,255,0.5)),
        rgba(255,255,255,0.46);
      border: 1px solid rgba(23, 33, 29, 0.08);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      margin-bottom: 18px;
    }
    .eyebrow {
      font-size: 12px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 10px;
    }
    h1 {
      font-size: clamp(2.2rem, 5vw, 4rem);
      line-height: 0.98;
      max-width: 10ch;
      margin-bottom: 12px;
    }
    .hero-copy {
      color: var(--muted);
      max-width: 58ch;
      line-height: 1.6;
    }
    .hero-meta {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 14px;
    }
    .chip {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255,255,255,0.74);
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
    }
    .summary-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .summary-tile {
      padding: 16px;
      border-radius: 20px;
      border: 1px solid var(--line);
      background: var(--surface-strong);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.6);
      min-height: 110px;
    }
    .summary-label {
      font-size: 11px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 12px;
    }
    .summary-value {
      font-size: 24px;
      line-height: 1;
      margin-bottom: 10px;
    }
    .summary-note {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }
    .section {
      margin-top: 18px;
      padding: 22px;
      border-radius: 26px;
      background: var(--surface);
      border: 1px solid rgba(23, 33, 29, 0.08);
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
    }
    .section-head {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: end;
      margin-bottom: 18px;
    }
    .section-title-wrap {
      max-width: 64ch;
    }
    .section-title {
      font-size: 24px;
      margin-bottom: 6px;
    }
    .section-note {
      color: var(--muted);
      line-height: 1.5;
      font-size: 12px;
    }
    .section-count {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .recommendation-list, .alert-list, .ops-list {
      display: grid;
      gap: 14px;
    }
    .recommendation, .alert-item, .ops-item {
      padding: 18px;
      border-radius: 22px;
      background: rgba(255,255,255,0.82);
      border: 1px solid var(--line);
      box-shadow: 0 12px 34px rgba(23, 33, 29, 0.05);
      animation: fadeUp 320ms ease;
    }
    .recommendation-top {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: start;
      margin-bottom: 14px;
    }
    .recommendation-symbol {
      font-size: 22px;
      line-height: 1;
      margin-bottom: 6px;
    }
    .recommendation-setup {
      color: var(--muted);
      font-size: 12px;
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 8px 12px;
      border-radius: 999px;
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      border: 1px solid transparent;
    }
    .status-open, .status-approved, .status-hold {
      background: var(--good-soft);
      color: var(--good);
      border-color: rgba(15, 123, 71, 0.2);
    }
    .status-pending_entry, .status-watch, .status-review_manual, .status-deferred {
      background: var(--warn-soft);
      color: var(--warn);
      border-color: rgba(154, 93, 0, 0.2);
    }
    .status-trim_winner, .status-tighten_stop, .status-add_on_pullback {
      background: rgba(15, 76, 92, 0.10);
      color: var(--accent);
      border-color: rgba(15, 76, 92, 0.18);
    }
    .status-exit_invalidation, .status-exit_stop, .status-exit_emergency, .status-exit_technical_failure, .status-triggered, .status-bad {
      background: var(--bad-soft);
      color: var(--bad);
      border-color: rgba(161, 42, 42, 0.2);
    }
    .recommendation.action-review_manual {
      background: linear-gradient(180deg, rgba(154, 93, 0, 0.08), rgba(255,255,255,0.88));
      border-color: rgba(154, 93, 0, 0.18);
    }
    .recommendation.action-trim_winner {
      background: linear-gradient(180deg, rgba(15, 123, 71, 0.08), rgba(255,255,255,0.88));
      border-color: rgba(15, 123, 71, 0.18);
    }
    .recommendation.action-tighten_stop {
      background: linear-gradient(180deg, rgba(15, 76, 92, 0.08), rgba(255,255,255,0.88));
      border-color: rgba(15, 76, 92, 0.18);
    }
    .recommendation.action-add_on_pullback {
      background: linear-gradient(180deg, rgba(36, 94, 60, 0.08), rgba(255,255,255,0.88));
      border-color: rgba(36, 94, 60, 0.18);
    }
    .recommendation.action-exit_action {
      background: linear-gradient(180deg, rgba(161, 42, 42, 0.08), rgba(255,255,255,0.88));
      border-color: rgba(161, 42, 42, 0.18);
    }
    .action-banner {
      margin-top: 2px;
      margin-bottom: 14px;
      padding: 10px 12px;
      border-radius: 14px;
      font-size: 12px;
      line-height: 1.5;
      border: 1px solid transparent;
    }
    .action-banner.review_manual {
      background: var(--warn-soft);
      color: var(--warn);
      border-color: rgba(154, 93, 0, 0.2);
    }
    .action-banner.trim_winner {
      background: var(--good-soft);
      color: var(--good);
      border-color: rgba(15, 123, 71, 0.2);
    }
    .action-banner.tighten_stop {
      background: rgba(15, 76, 92, 0.10);
      color: var(--accent);
      border-color: rgba(15, 76, 92, 0.18);
    }
    .action-banner.add_on_pullback {
      background: rgba(36, 94, 60, 0.10);
      color: #245e3c;
      border-color: rgba(36, 94, 60, 0.18);
    }
    .action-banner.exit_action {
      background: var(--bad-soft);
      color: var(--bad);
      border-color: rgba(161, 42, 42, 0.2);
    }
    .action-banner.default {
      background: rgba(23, 33, 29, 0.05);
      color: var(--ink);
      border-color: rgba(23, 33, 29, 0.10);
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .metric {
      padding: 12px;
      border-radius: 16px;
      background: rgba(15, 76, 92, 0.05);
      border: 1px solid rgba(15, 76, 92, 0.08);
    }
    .metric-label {
      font-size: 10px;
      color: var(--muted);
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .metric-value {
      font-size: 15px;
      line-height: 1.1;
    }
    .metric-value.good { color: var(--good); }
    .metric-value.bad { color: var(--bad); }
    .detail-toggle {
      margin-top: 10px;
      border-top: 1px dashed rgba(23, 33, 29, 0.12);
      padding-top: 10px;
    }
    .detail-toggle summary {
      cursor: pointer;
      list-style: none;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--accent);
      font-size: 12px;
      font-weight: 600;
      user-select: none;
    }
    .detail-toggle summary::-webkit-details-marker {
      display: none;
    }
    .detail-toggle summary::before {
      content: "+";
      display: inline-flex;
      width: 18px;
      height: 18px;
      border-radius: 999px;
      align-items: center;
      justify-content: center;
      background: rgba(15, 76, 92, 0.08);
      border: 1px solid rgba(15, 76, 92, 0.12);
      font-size: 12px;
      line-height: 1;
    }
    .detail-toggle[open] summary::before {
      content: "−";
    }
    .detail-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-top: 12px;
    }
    .detail-block {
      padding: 12px 13px;
      border-radius: 16px;
      background: rgba(23, 33, 29, 0.035);
      border: 1px solid rgba(23, 33, 29, 0.06);
    }
    .detail-label {
      font-size: 10px;
      color: var(--muted);
      letter-spacing: 0.1em;
      text-transform: uppercase;
      margin-bottom: 8px;
    }
    .detail-text {
      font-size: 12px;
      line-height: 1.5;
    }
    .ops-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 18px;
    }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      background: rgba(23, 33, 29, 0.04);
      padding: 12px;
      border-radius: 14px;
      overflow: auto;
      max-height: 320px;
      margin: 0;
    }
    .empty {
      padding: 24px;
      border-radius: 18px;
      border: 1px dashed rgba(23, 33, 29, 0.15);
      color: var(--muted);
      background: rgba(255,255,255,0.4);
    }
    @keyframes fadeUp {
      from { opacity: 0; transform: translateY(8px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @media (max-width: 1100px) {
      .hero, .summary-grid, .ops-grid, .detail-grid { grid-template-columns: 1fr; }
      .metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    @media (max-width: 720px) {
      .wrap { padding: 16px; }
      .hero, .section { padding: 18px; border-radius: 22px; }
      .metrics { grid-template-columns: 1fr 1fr; }
      .recommendation-top { flex-direction: column; align-items: start; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div>
        <div class="eyebrow">Stockey Operator Dashboard</div>
        <h1>Recommendations, watch state, exits, and live triggers.</h1>
        <div class="hero-copy">
          Track what is currently invested, what is being watched, what has moved to exit or review, and which live alerts need attention. Each recommendation includes the active regime, overlay, screener provenance, and setup context in plain language. Entry is treated as immediate at recommendation price with no slippage.
        </div>
        <div class="hero-meta" id="meta"></div>
      </div>
      <div class="summary-grid" id="summary"></div>
    </section>

    <section class="section">
      <div class="section-head">
        <div class="section-title-wrap">
          <h2 class="section-title">Action Recommendations</h2>
          <div class="section-note">Single resolved symbol-level actions from the consolidated action engine. This is the operator queue for buy, buy more, partial sell, sell, and high-priority manual intervention.</div>
        </div>
        <div class="section-count" id="top-action-count"></div>
      </div>
      <div class="recommendation-list" id="top_action_recommendations"></div>
    </section>

    <section class="section">
      <div class="section-head">
        <div class="section-title-wrap">
          <h2 class="section-title">Today's Recommendations</h2>
          <div class="section-note">Latest portfolio recommendations issued today, shown first with entry, current price, profit since recommendation, and supporting context.</div>
        </div>
        <div class="section-count" id="today-count"></div>
      </div>
      <div class="recommendation-list" id="today_recommendations"></div>
    </section>

    <section class="section">
      <div class="section-head">
        <div class="section-title-wrap">
          <h2 class="section-title">Current Recommendations</h2>
          <div class="section-note">Active or pending positions with entry date, assumed entry, current price, profit since recommendation, exit strategy, and latest supporting context.</div>
        </div>
        <div class="section-count" id="current-count"></div>
      </div>
      <div class="recommendation-list" id="current_recommendations"></div>
    </section>

    <section class="section">
      <div class="section-head">
        <div class="section-title-wrap">
          <h2 class="section-title">Watch Recommendations</h2>
          <div class="section-note">Ideas not yet in position. Focus here is entry zone, current price, why they are being watched, and whether recent news or announcements changed the setup.</div>
        </div>
        <div class="section-count" id="watch-count"></div>
      </div>
      <div class="recommendation-list" id="watch_recommendations"></div>
    </section>

    <section class="section">
      <div class="section-head">
        <div class="section-title-wrap">
          <h2 class="section-title">Exited Recommendations</h2>
          <div class="section-note">True exit-triggered recommendations where the lifecycle engine asked to get out based on stop, invalidation, or hard thesis break conditions.</div>
        </div>
        <div class="section-count" id="exited-count"></div>
      </div>
      <div class="recommendation-list" id="exited_recommendations"></div>
    </section>

    <section class="section">
      <div class="section-head">
        <div class="section-title-wrap">
          <h2 class="section-title">Needs Action</h2>
          <div class="section-note">Non-exit lifecycle actions such as manual review, tighten stop, or trim winner. This is the queue that still needs an operator decision.</div>
        </div>
        <div class="section-count" id="action-count"></div>
      </div>
      <div class="recommendation-list" id="action_recommendations"></div>
    </section>

    <section class="section">
      <div class="section-head">
        <div class="section-title-wrap">
          <h2 class="section-title">Live Alerts</h2>
          <div class="section-note">Immediate triggers from price, invalidation, stop, or breakout conditions.</div>
        </div>
        <div class="section-count" id="alert-count"></div>
      </div>
      <div class="alert-list" id="alerts"></div>
    </section>

    <section class="section">
      <div class="section-head">
        <div class="section-title-wrap">
          <h2 class="section-title">Operator Console</h2>
          <div class="section-note">Runtime process visibility, cron output, and sync state in one place.</div>
        </div>
      </div>
      <div class="ops-grid">
        <div>
          <h3 style="margin-bottom:12px;">Running Processes</h3>
          <div class="ops-list" id="runtime_processes"></div>
        </div>
        <div>
          <h3 style="margin-bottom:12px;">Cron Logs</h3>
          <div class="ops-list" id="cron_status"></div>
        </div>
      </div>
      <div style="margin-top:18px;">
        <h3 style="margin-bottom:12px;">Sync State</h3>
        <pre id="sync_state"></pre>
      </div>
    </section>
  </div>
  <script>
    async function loadDashboard() {
      const res = await fetch("dashboard.json?_ts=" + Date.now());
      const data = await res.json();
      const summary = data.summary || {};
      document.getElementById("meta").innerHTML = [
        chip("Generated", data.generated_at || "-"),
        chip("Asof", data.asof_date || "latest"),
        chip("Regimes", (summary.regimes || []).join(", ") || "n/a"),
        chip("Overlays", (summary.overlays || []).join(", ") || "n/a"),
        chip("Overlay Why", (summary.overlay_reasons || []).join(" | ") || "n/a")
      ].join("");

      document.getElementById("summary").innerHTML = [
        summaryTile("Action Queue", summary.top_action_count || 0, "Resolved buy, buy more, partial sell, sell, and review actions that need attention first."),
        summaryTile("Today", summary.today_count || 0, "Recommendations issued on the latest recommendation date."),
        summaryTile("Current", summary.current_count || 0, `${summary.open_count || 0} open, ${summary.pending_count || 0} pending, ${summary.positive_count || 0} positive since entry.`),
        summaryTile("Watch", summary.watch_count || 0, "Ideas waiting for the right entry, event confirmation, or price location."),
        summaryTile("Exited", summary.exited_count || 0, "Positions where the system has emitted an actual exit action."),
        summaryTile("Needs Action", summary.action_count || 0, "Manual review, trim, or stop-adjustment actions waiting on operator attention."),
        summaryTile("Live Alerts", summary.alert_count || 0, "Immediate triggers from price or state changes that may need operator attention.")
      ].join("");

      const topActions = Array.isArray(data.top_action_recommendations) ? data.top_action_recommendations : [];
      document.getElementById("top-action-count").textContent = `${topActions.length} rows`;
      document.getElementById("top_action_recommendations").innerHTML = renderRecommendationCards(topActions, { showPnl: true, watchMode: false, actionMode: true });

      const today = Array.isArray(data.today_recommendations) ? data.today_recommendations : [];
      document.getElementById("today-count").textContent = `${today.length} rows`;
      document.getElementById("today_recommendations").innerHTML = renderRecommendationCards(today, { showPnl: true, watchMode: false, actionMode: false });

      const current = Array.isArray(data.current_recommendations) ? data.current_recommendations : [];
      document.getElementById("current-count").textContent = `${current.length} rows`;
      document.getElementById("current_recommendations").innerHTML = renderRecommendationCards(current, { showPnl: true, watchMode: false, actionMode: false });

      const watch = Array.isArray(data.watch_recommendations) ? data.watch_recommendations : [];
      document.getElementById("watch-count").textContent = `${watch.length} rows`;
      document.getElementById("watch_recommendations").innerHTML = renderRecommendationCards(watch, { showPnl: false, watchMode: true, actionMode: false });

      const exited = Array.isArray(data.exited_recommendations) ? data.exited_recommendations : [];
      document.getElementById("exited-count").textContent = `${exited.length} rows`;
      document.getElementById("exited_recommendations").innerHTML = renderRecommendationCards(exited, { showPnl: true, watchMode: false, actionMode: true });

      const actions = Array.isArray(data.action_recommendations) ? data.action_recommendations : [];
      document.getElementById("action-count").textContent = `${actions.length} rows`;
      document.getElementById("action_recommendations").innerHTML = renderRecommendationCards(actions, { showPnl: true, watchMode: false, actionMode: true });

      const alerts = Array.isArray(data.alerts) ? data.alerts : [];
      document.getElementById("alert-count").textContent = `${alerts.length} rows`;
      document.getElementById("alerts").innerHTML = renderAlerts(alerts);

      const runtimeProcesses = Array.isArray(data.runtime_processes) ? data.runtime_processes : [];
      document.getElementById("runtime_processes").innerHTML = renderOps(runtimeProcesses.map(row => `
        <div class="ops-item">
          <div class="detail-label">Process</div>
          <div class="detail-text"><strong>${escapeHtml(row.command || "-")}</strong><br>PID ${escapeHtml(String(row.pid || "-"))} | ${escapeHtml(String(row.elapsed_seconds || 0))}s running</div>
        </div>
      `));

      const cronStatus = Array.isArray(data.cron_status) ? data.cron_status : [];
      document.getElementById("cron_status").innerHTML = renderOps(cronStatus.map(row => `
        <div class="ops-item">
          <div class="detail-label">${escapeHtml(row.log_file || "-")}</div>
          <div class="detail-text">${escapeHtml(row.modified_at || "-")} | ${escapeHtml(String(row.size_bytes || 0))} bytes</div>
          <pre>${escapeHtml(row.tail || "")}</pre>
        </div>
      `));

      document.getElementById("sync_state").textContent = JSON.stringify(data.sync_state || [], null, 2);
    }

    function chip(label, value) {
      return `<span class="chip"><strong>${escapeHtml(label)}</strong> ${escapeHtml(value)}</span>`;
    }

    function summaryTile(label, value, note) {
      return `
        <div class="summary-tile">
          <div class="summary-label">${escapeHtml(label)}</div>
          <div class="summary-value">${escapeHtml(String(value))}</div>
          <div class="summary-note">${escapeHtml(note)}</div>
        </div>
      `;
    }

    function renderRecommendationCards(rows, options) {
      if (!rows.length) {
        return '<div class="empty">No rows.</div>';
      }
      return rows.map(row => {
        const pnl = numberOrNull(row.pnl_pct);
        const pnlClass = pnl === null ? '' : (pnl >= 0 ? 'good' : 'bad');
        const entryLabel = options.watchMode ? 'Entry Zone' : 'Entry';
        const pnlLabel = options.watchMode ? 'Watch Gap' : 'Profit';
        const pnlValue = options.watchMode ? (row.current_value_note || '-') : formatPct(pnl);
        const investScore = formatScore(row.invest_score_pct);
        const actionKey = String(row.status || '').toLowerCase().replace(/[^a-z0-9]+/g, '_');
        const statusClass = `status-${actionKey}`;
        const actionTone = actionCardTone(actionKey);
        const actionBanner = options.actionMode ? actionBannerMarkup(actionKey, row.reason_detail || row.reason) : '';
        return `
          <article class="recommendation ${options.actionMode ? `action-${actionTone}` : ''}">
            <div class="recommendation-top">
              <div>
                <div class="recommendation-symbol">${escapeHtml(row.symbol || '-')}</div>
                <div class="recommendation-setup">${escapeHtml(row.setup_name || row.setup_id || '-')}</div>
                <div class="recommendation-setup">${escapeHtml(row.setup_family || '')}</div>
              </div>
              <div class="status-pill ${statusClass}">${escapeHtml(row.status || 'unknown')}</div>
            </div>
            ${actionBanner}
            <div class="metrics">
              <div class="metric">
                <div class="metric-label">Entry Date</div>
                <div class="metric-value">${escapeHtml(row.entry_date || '-')}</div>
              </div>
              <div class="metric">
                <div class="metric-label">${escapeHtml(entryLabel)}</div>
                <div class="metric-value">${formatPrice(row.entry_price)}</div>
              </div>
              <div class="metric">
                <div class="metric-label">Current</div>
                <div class="metric-value">${formatPrice(row.current_price)}</div>
              </div>
              <div class="metric">
                <div class="metric-label">${escapeHtml(pnlLabel)}</div>
                <div class="metric-value ${pnlClass}">${escapeHtml(pnlValue)}</div>
              </div>
              <div class="metric">
                <div class="metric-label">Invest Score</div>
                <div class="metric-value">${escapeHtml(investScore)}</div>
              </div>
            </div>
            <details class="detail-toggle">
              <summary>Show details</summary>
              <div class="detail-grid">
                ${detailBlock('Reason', row.reason)}
                ${detailBlock('Reason Detail', row.reason_detail)}
                ${detailBlock('Technical View', row.technical_context)}
                ${detailBlock('Market Regime', row.market_context)}
                ${detailBlock('Screeners', row.screener_context)}
                ${detailBlock('Setup Context', row.setup_context)}
                ${detailBlock('Exit Strategy', row.exit_strategy)}
                ${detailBlock('News Summary', row.news_summary)}
                ${detailBlock('Announcement Summary', row.announcement_summary)}
                ${detailBlock('Action Plan', row.action_summary)}
                ${detailBlock('Execution Intent', row.execution_intent)}
                ${detailBlock(options.actionMode ? 'Action Note' : 'Performance Note', row.current_value_note)}
              </div>
            </details>
          </article>
        `;
      }).join('');
    }

    function actionCardTone(actionKey) {
      if (actionKey === 'review_manual') return 'review_manual';
      if (actionKey === 'trim_winner') return 'trim_winner';
      if (actionKey === 'add_on_pullback') return 'add_on_pullback';
      if (actionKey === 'tighten_stop') return 'tighten_stop';
      if (actionKey.startsWith('exit_')) return 'exit_action';
      return 'default';
    }

    function actionBannerMarkup(actionKey, detail) {
      const copyMap = {
        review_manual: 'Manual intervention required before the system can manage this idea automatically.',
        trim_winner: 'Profit has expanded enough that the system wants to reduce exposure.',
        add_on_pullback: 'The pullback looks constructive enough that the system wants to add exposure.',
        tighten_stop: 'Risk has shifted enough that the stop should be tightened.',
        exit_action: 'The live position should be closed because the exit condition has already fired.',
      };
      const tone = actionCardTone(actionKey);
      const headline = {
        review_manual: 'Operator review required',
        trim_winner: 'Profit-taking action',
        add_on_pullback: 'Scale-in action',
        tighten_stop: 'Risk-control action',
        exit_action: 'Exit action',
        default: 'Lifecycle action',
      }[tone];
      const body = detail || copyMap[tone] || 'The lifecycle engine emitted a follow-up action.';
      return `
        <div class="action-banner ${tone}">
          <strong>${escapeHtml(headline)}</strong><br>
          ${escapeHtml(body)}
        </div>
      `;
    }

    function renderAlerts(rows) {
      if (!rows.length) {
        return '<div class="empty">No live alerts.</div>';
      }
      return rows.map(row => {
        const type = String(row.alert_type || '-');
        const statusClass = type.includes('INVALIDATION') || type.includes('STOP') ? 'status-exit_stop' : 'status-open';
        return `
          <div class="alert-item">
            <div class="recommendation-top" style="margin-bottom:8px;">
              <div>
                <div style="font-size:20px; margin-bottom:6px;">${escapeHtml(row.symbol || '-')}</div>
                <div class="recommendation-setup">${escapeHtml(row.setup_id || '-')}</div>
              </div>
              <div class="status-pill ${statusClass}">${escapeHtml(type)}</div>
            </div>
            <div class="detail-grid">
              ${detailBlock('Observed', row.observed_at)}
              ${detailBlock('Last Price', formatPrice(row.last_price))}
              ${detailBlock('Reason', row.alert_reason)}
              ${detailBlock('Notes', row.alert_detail || row.alert_context || '-')}
            </div>
          </div>
        `;
      }).join('');
    }

    function renderOps(items) {
      if (!items.length) {
        return '<div class="empty">No rows.</div>';
      }
      return items.join('');
    }

    function detailBlock(label, value) {
      return `
        <div class="detail-block">
          <div class="detail-label">${escapeHtml(label)}</div>
          <div class="detail-text">${escapeHtml(value || '-')}</div>
        </div>
      `;
    }

    function formatPrice(value) {
      const number = numberOrNull(value);
      return number === null ? '-' : number.toFixed(2);
    }

    function formatCurrency(value) {
      const number = numberOrNull(value);
      return number === null ? '-' : new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 }).format(number);
    }

    function formatScore(value) {
      const number = numberOrNull(value);
      if (number === null) return '-';
      return `${Math.max(0, Math.min(100, Math.round(number)))}/100`;
    }

    function formatPct(value) {
      const number = numberOrNull(value);
      return number === null ? '-' : `${number.toFixed(2)}%`;
    }

    function numberOrNull(value) {
      if (value === null || value === undefined || value === '') return null;
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }

    function escapeHtml(value) {
      return String(value ?? '-').replace(/[&<>"]/g, s => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[s]));
    }

    loadDashboard();
    setInterval(loadDashboard, 30000);
</script>
</body>
</html>"""


def write_live_dashboard(*, output_dir: str | Path = DEFAULT_OUTPUT_DIR, asof_date: pd.Timestamp | None = None) -> dict[str, Any]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    payload = build_live_dashboard_payload(asof_date=asof_date, output_dir=output_path)
    (output_path / "dashboard.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str, allow_nan=False),
        encoding="utf-8",
    )
    (output_path / "index.html").write_text(render_html(payload), encoding="utf-8")
    return {
        "status": "ok",
        "output_dir": str(output_path),
        "json_path": str(output_path / "dashboard.json"),
        "html_path": str(output_path / "index.html"),
        "today_recommendation_rows": len(payload.get("today_recommendations") or []),
        "current_recommendation_rows": len(payload.get("current_recommendations") or []),
        "watch_recommendation_rows": len(payload.get("watch_recommendations") or []),
        "exited_recommendation_rows": len(payload.get("exited_recommendations") or []),
        "action_recommendation_rows": len(payload.get("action_recommendations") or []),
        "dashboard_rows": len(payload.get("dashboard") or []),
        "portfolio_rows": len(payload.get("portfolio") or []),
        "watchlist_rows": len(payload.get("watchlist") or []),
        "candidate_rows": len(payload.get("candidates") or []),
        "lifecycle_rows": len(payload.get("lifecycle") or []),
        "rebalance_rows": len(payload.get("rebalance") or []),
        "alert_rows": len(payload.get("alerts") or []),
        "watch_event_rows": len(payload.get("watch_events") or []),
        "operator_feed_rows": len(payload.get("operator_feed") or []),
        "runtime_process_rows": len(payload.get("runtime_processes") or []),
        "cron_status_rows": len(payload.get("cron_status") or []),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write a simple auto-refresh live advisory dashboard as static HTML and JSON.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Optional asof date in YYYY-MM-DD")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof_date = _normalize_utc_arg_timestamp(args.date)
    result = write_live_dashboard(output_dir=args.output_dir, asof_date=asof_date)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
