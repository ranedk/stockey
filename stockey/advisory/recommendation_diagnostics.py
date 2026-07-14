from __future__ import annotations

import argparse
import json
import os
import shlex
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from advisory.action_recommender import TABLE_NAME as ACTION_RECOMMENDATIONS_TABLE
from advisory.context_overlay_reliability_report import load_persisted_reliability_report
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.sync_state import load_sync_state
from advisory.technical_engine import evaluate_pre_entry_state
from advisory.technical_threshold_calibration import (
    DEFAULT_MIN_SIGNALS as TECHNICAL_THRESHOLD_MIN_SIGNALS,
    SUMMARY_TABLE as TECHNICAL_THRESHOLD_SUMMARY_TABLE,
    infer_calibration_setup_archetype,
)
from advisory.watchlist_builder import CONTEXT_OVERLAY_WATCHLIST_BUILDER_VERSION
from utils.db import sql_to_df
from utils.sync import parse_datetime_arg


POSITIVE_ACTIONS = {"BUY", "BUY_MORE"}
BROKER_ACTIONS = {"BUY", "BUY_MORE", "SELL", "PARTIAL_SELL"}
EXIT_OR_RISK_REDUCTION_ACTIONS = {"SELL", "PARTIAL_SELL", "TIGHTEN_STOP", "REDUCE_EXPOSURE_REVIEW"}
MARKET_CONTEXT_RISK_OFF_LABELS = {"RISK_OFF", "CRISIS", "STRESS", "HIGH_RISK"}
PRIMARY_CAUSE_DIAGNOSIS_OVERRIDES = {
    "diagnostic_requires_advisory_rerun",
    "diagnostic_limited_evidence",
    "candidate_source_stale_for_asof",
    "candidate_source_date_mismatch",
    "candidate_source_missing",
    "candidate_source_unavailable",
    "post_advisory_watcher_action_refresh_pending",
    "post_advisory_full_reconciliation_pending",
    "upstream_pass_now_without_technical_confirmation",
    "confirmed_technical_candidates_lost_downstream",
}
SIGNAL_REFRESH_ACTION_REFRESH_ACTIONS = {
    "WATCH",
    "BUY",
    "BUY_MORE",
    "BUY_TRIGGERED",
    "ADD_ON_PULLBACK",
    "MANUAL_REVIEW",
    "REVIEW_MANUAL",
    "REDUCE_EXPOSURE_REVIEW",
    "REDUCE_REVIEW",
    "TIGHTEN_STOP",
}
CANDIDATES_TABLE = "advisory_candidates"
REJECTIONS_TABLE = "advisory_candidate_rejections"
ALLOCATIONS_TABLE = "advisory_allocations"
PORTFOLIO_TABLE = "advisory_portfolio_orders"
WATCHLIST_TABLE = "advisory_watchlist"
TECHNICAL_DAILY_TABLE = "advisory_technical_daily"
TECHNICAL_REFRESH_STATUS_TABLE = "advisory_technical_feature_refresh_status"
DHAN_OHLCV_DAILY_TABLE = "dhan_ohlcv_daily"
COMPANY_MASTER_TABLE = "company_master"
DHAN_MASTER_TABLE = "master_dhan_instruments"
TRADING_DAYS_TABLE = "dim_trading_days"
SIGNAL_REFRESH_TABLE = "advisory_signal_refresh_actions"
WAIT_SIGNAL_MATCHES_TABLE = "advisory_wait_signal_matches"
ACTION_REFRESH_SYNC_SOURCE = "continuous_watch:action_refresh"
MARKET_REGIME_TABLE = "advisory_market_regime"
SIGNAL_QUALITY_SUMMARY_TABLE = "advisory_signal_quality_eval_summary"
CONTEXT_OVERLAY_SIGNAL_REFRESH_SYNC_SOURCE = "advisory:signal_refresh:context_overlays"
CONTEXT_RELIABILITY_MAX_AGE_DAYS = 14
RULE_ENGINE_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV = "RULE_ENGINE_REGIME_LABEL_HARD_BLOCK_ENABLED"
RULE_ENGINE_OVERLAY_LABEL_HARD_BLOCK_ENABLED_ENV = "RULE_ENGINE_OVERLAY_LABEL_HARD_BLOCK_ENABLED"
ACTION_MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV = "ACTION_MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED"
ACTION_MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED_ENV = "ACTION_MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED"
ACTION_MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED_ENV = "ACTION_MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED"
ACTION_MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED_ENV = "ACTION_MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED"
HYPOTHESIS_MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV = "HYPOTHESIS_MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED"
HYPOTHESIS_MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED_ENV = "HYPOTHESIS_MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED"
HYPOTHESIS_MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED_ENV = "HYPOTHESIS_MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED"
HYPOTHESIS_MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED_ENV = "HYPOTHESIS_MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED"
MODULE_DIR = Path(__file__).resolve().parent
CODE_FRESHNESS_FILES = {
    "recommendation_diagnostics": Path(__file__).resolve(),
    "rule_engine": MODULE_DIR / "rule_engine.py",
    "risk_engine": MODULE_DIR / "risk_engine.py",
    "portfolio_engine": MODULE_DIR / "portfolio_engine.py",
    "action_recommender": MODULE_DIR / "action_recommender.py",
}


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    return str(value)


def _jsonish(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_json_parse_failed",
            source="jsonish",
            severity="warn",
            reason="Recommendation diagnostics could not parse a JSON-like field and used the caller default.",
            error=exc,
            metadata={"value_type": value.__class__.__name__},
        )
        return default


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _num(value: Any) -> float | None:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return None
    return float(numeric)


def _boolish(value: Any) -> bool:
    if value is None:
        return False
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y"}


def _map_signal_refresh_action_for_diagnostics(action: Any) -> str | None:
    raw_action = str(action or "").strip().upper()
    if raw_action in {"WATCH", "BUY", "BUY_MORE", "BUY_TRIGGERED", "ADD_ON_PULLBACK"}:
        return "WATCH"
    if raw_action in {"REDUCE_EXPOSURE_REVIEW", "REDUCE_REVIEW"}:
        return "REDUCE_EXPOSURE_REVIEW"
    if raw_action == "TIGHTEN_STOP":
        return "TIGHTEN_STOP"
    if raw_action in {"MANUAL_REVIEW", "REVIEW_MANUAL"}:
        return "MANUAL_REVIEW"
    return None


def _format_archetype_readiness(items: Any, *, limit: int = 5) -> str:
    if not isinstance(items, list) or not items:
        return "[]"
    parts = []
    for item in items[: int(limit)]:
        if not isinstance(item, dict):
            continue
        parts.append(
            "{name}:c={c}/confirmed={confirmed}/watch={watch}/ignore={ignore}/final={final}".format(
                name=item.get("technical_setup_archetype"),
                c=item.get("candidate_count", 0),
                confirmed=item.get("confirmed_entry_count", 0),
                watch=item.get("watch_or_near_entry_count", 0),
                ignore=item.get("ignore_or_reject_count", 0),
                final=item.get("final_action_counts", {}),
            )
        )
    return "[" + "; ".join(parts) + "]"


def _format_trigger_blockers(summary: Any, *, limit: int = 5) -> str:
    if not isinstance(summary, dict) or not summary:
        return "{}"
    blockers = summary.get("top_blockers")
    triggers = summary.get("nearest_trigger_counts")
    samples = summary.get("samples")
    return (
        "{status="
        + str(summary.get("status"))
        + ", nearest="
        + str(triggers or {})
        + ", top_blockers="
        + str((blockers or [])[: int(limit)] if isinstance(blockers, list) else blockers)
        + ", samples="
        + str((samples or [])[:3] if isinstance(samples, list) else samples)
        + "}"
    )


def _format_buy_readiness_blockers(summary: Any, *, limit: int = 5) -> str:
    if not isinstance(summary, dict) or not summary:
        return "{}"
    blockers = summary.get("top_blockers")
    categories = summary.get("category_counts")
    metrics = summary.get("metric_counts")
    samples = summary.get("samples")
    return (
        "{status="
        + str(summary.get("status"))
        + ", categories="
        + str(categories or {})
        + ", top_blockers="
        + str((blockers or [])[: int(limit)] if isinstance(blockers, list) else blockers)
        + ", metrics="
        + str(metrics or {})
        + ", samples="
        + str((samples or [])[:3] if isinstance(samples, list) else samples)
        + "}"
    )


def _format_candidate_survival_matrix(items: Any, *, limit: int = 6) -> str:
    if not isinstance(items, list) or not items:
        return "[]"
    parts = []
    for item in items[: int(limit)]:
        if not isinstance(item, dict):
            continue
        parts.append(
            "{candidate}/{technical}/{trigger}-> {action}({source}) n={count} pass={rule_pass} watch={watch}".format(
                candidate=item.get("candidate_state"),
                technical=item.get("technical_state"),
                trigger=item.get("technical_trigger_type"),
                action=item.get("final_action"),
                source=item.get("final_action_source"),
                count=item.get("row_count", 0),
                rule_pass=item.get("rule_pass_count", 0),
                watch=item.get("watch_enabled_count", 0),
            )
        )
    return "[" + "; ".join(parts) + "]"


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _timestamp(value: Any) -> pd.Timestamp | None:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(ts) else ts


def _count(counter: Counter[str], key: str, amount: int = 1) -> None:
    if key:
        counter[key] += int(amount)


def _exit_only_action_mix(action_counts: Counter[str] | dict[str, int] | None) -> bool:
    action_counts = action_counts or {}
    active_actions = {
        str(action or "").strip().upper()
        for action, count in action_counts.items()
        if int(count or 0) > 0 and str(action or "").strip()
    }
    return bool(active_actions) and active_actions.issubset(EXIT_OR_RISK_REDUCTION_ACTIONS)


def _latest_asof_date() -> pd.Timestamp | None:
    df = sql_to_df(f"SELECT MAX(asof_date) AS asof_date FROM {ACTION_RECOMMENDATIONS_TABLE}")
    if df.empty:
        return None
    ts = pd.to_datetime(df["asof_date"].iloc[0], utc=True, errors="coerce")
    return None if pd.isna(ts) else ts.normalize()


def _table_exists(table_name: str) -> bool:
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


def _table_columns(table_name: str) -> set[str]:
    if not _table_exists(table_name):
        return set()
    df = sql_to_df(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        """,
        params=(table_name,),
    )
    if df.empty or "column_name" not in df.columns:
        return set()
    return {str(item).strip() for item in df["column_name"].dropna().tolist()}


def _optional_select(column_name: str, columns: set[str], *, cast: str = "TEXT") -> str:
    if column_name in columns:
        return column_name
    return f"NULL::{cast} AS {column_name}"


def _optional_select_alias(alias: str, column_name: str, columns: set[str], *, cast: str = "TEXT", output_name: str | None = None) -> str:
    output = output_name or column_name
    if column_name in columns:
        return f"{alias}.{column_name} AS {output}"
    return f"NULL::{cast} AS {output}"


def _load_dhan_identity_availability(symbols: list[str]) -> dict[str, Any]:
    normalized_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    if not normalized_symbols:
        return {
            "status": "no_symbols",
            "resolved_symbols": [],
            "missing_symbols": [],
            "dhan_identity_coverage_count": 0,
            "missing_dhan_identity_count": 0,
        }
    resolved: set[str] = set()
    sources: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    try:
        if _table_exists(COMPANY_MASTER_TABLE):
            company_df = sql_to_df(
                f"""
                SELECT DISTINCT UPPER(TRIM(COALESCE(nse_ticker, bse_ticker))) AS symbol
                FROM {COMPANY_MASTER_TABLE}
                WHERE (UPPER(TRIM(COALESCE(nse_ticker, ''))) = ANY(%s)
                       OR UPPER(TRIM(COALESCE(bse_ticker, ''))) = ANY(%s))
                  AND (dhan_nse_id IS NOT NULL OR dhan_bse_id IS NOT NULL)
                """,
                params=(normalized_symbols, normalized_symbols),
            )
            if not company_df.empty and "symbol" in company_df.columns:
                company_symbols = {
                    str(item or "").strip().upper()
                    for item in company_df["symbol"].dropna().tolist()
                    if str(item or "").strip()
                }
                resolved.update(company_symbols)
                sources["company_master"] += len(company_symbols)
    except Exception as exc:
        errors.append({"source": COMPANY_MASTER_TABLE, "error_type": exc.__class__.__name__, "error": str(exc)[:300]})
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_dhan_identity_company_master_failed",
            source=COMPANY_MASTER_TABLE,
            severity="warn",
            reason="Recommendation diagnostics could not check company-master Dhan identity coverage.",
            error=exc,
            metadata={"symbol_count": len(normalized_symbols)},
        )
    try:
        if _table_exists(DHAN_MASTER_TABLE):
            dhan_df = sql_to_df(
                f"""
                SELECT DISTINCT UPPER(TRIM(underlying_symbol)) AS symbol
                FROM {DHAN_MASTER_TABLE}
                WHERE valid_to IS NULL
                  AND exch_id = 'NSE'
                  AND instrument = 'EQUITY'
                  AND instrument_type = 'ES'
                  AND underlying_symbol IS NOT NULL
                  AND UPPER(TRIM(underlying_symbol)) = ANY(%s)
                """,
                params=(normalized_symbols,),
            )
            if not dhan_df.empty and "symbol" in dhan_df.columns:
                dhan_symbols = {
                    str(item or "").strip().upper()
                    for item in dhan_df["symbol"].dropna().tolist()
                    if str(item or "").strip()
                }
                resolved.update(dhan_symbols)
                sources["master_dhan_instruments"] += len(dhan_symbols)
    except Exception as exc:
        errors.append({"source": DHAN_MASTER_TABLE, "error_type": exc.__class__.__name__, "error": str(exc)[:300]})
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_dhan_identity_master_failed",
            source=DHAN_MASTER_TABLE,
            severity="warn",
            reason="Recommendation diagnostics could not check Dhan master identity coverage.",
            error=exc,
            metadata={"symbol_count": len(normalized_symbols)},
        )
    missing = sorted(set(normalized_symbols) - resolved)
    status = "identity_check_failed" if errors and not resolved else "identity_available" if not missing else "identity_partial"
    return {
        "status": status,
        "resolved_symbols": sorted(resolved),
        "missing_symbols": missing,
        "dhan_identity_coverage_count": int(len(resolved)),
        "missing_dhan_identity_count": int(len(missing)),
        "missing_dhan_identity_sample": missing[:10],
        "identity_sources": dict(sources),
        "errors": errors,
    }


def _load_latest_technical_refresh_status(symbols: list[str], asof_date: pd.Timestamp) -> dict[str, Any]:
    normalized_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    if not normalized_symbols:
        return {"status": "no_symbols", "issue_count": 0, "issue_symbols": []}
    if not _table_exists(TECHNICAL_REFRESH_STATUS_TABLE):
        return {
            "status": "refresh_status_table_missing",
            "issue_count": 0,
            "issue_symbols": [],
            "operator_action": "Run advisory.technical_features after this code version to create technical refresh status rows.",
        }
    try:
        df = sql_to_df(
            f"""
            SELECT DISTINCT ON (UPPER(TRIM(symbol)), stage)
                UPPER(TRIM(symbol)) AS symbol,
                asof_date,
                stage,
                status,
                action,
                reason,
                rows,
                error_type,
                error_text,
                raw_json,
                load_ts
            FROM {TECHNICAL_REFRESH_STATUS_TABLE}
            WHERE asof_date <= %s
              AND UPPER(TRIM(symbol)) = ANY(%s)
            ORDER BY UPPER(TRIM(symbol)), stage, asof_date DESC, load_ts DESC NULLS LAST
            """,
            params=(asof_date, normalized_symbols),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_technical_refresh_status_failed",
            source=TECHNICAL_REFRESH_STATUS_TABLE,
            severity="warn",
            reason="Recommendation diagnostics could not load latest technical refresh status rows.",
            error=exc,
            metadata={"asof_date": asof_date.isoformat(), "symbol_count": len(normalized_symbols)},
        )
        return {"status": "refresh_status_load_failed", "issue_count": 0, "issue_symbols": [], "error_type": exc.__class__.__name__}
    if df.empty:
        return {
            "status": "refresh_status_empty",
            "issue_count": 0,
            "issue_symbols": [],
            "operator_action": "No technical refresh status rows exist for active context-watch symbols; rerun advisory.technical_features under the current code.",
        }
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["status"] = df["status"].astype("string").str.strip().str.lower()
    issue_mask = df["status"].isin(["issue", "stale", "skipped_with_warning"])
    issue_df = df.loc[issue_mask].copy()
    issue_symbols = sorted(issue_df["symbol"].dropna().drop_duplicates().tolist())
    issue_rows: list[dict[str, Any]] = []
    samples = []
    for row in issue_df.head(200).to_dict(orient="records"):
        item = {
            "symbol": _text(row.get("symbol")),
            "stage": _text(row.get("stage")),
            "status": _text(row.get("status")),
            "reason": _text(row.get("reason")),
            "error_type": _text(row.get("error_type")),
            "error_text": _text(row.get("error_text")),
            "rows": _num(row.get("rows")),
        }
        issue_rows.append(item)
        if len(samples) < 10:
            samples.append(item)
    return {
        "status": "refresh_status_has_issues" if issue_symbols else "refresh_status_ok",
        "row_count": int(len(df)),
        "issue_count": int(len(issue_df)),
        "issue_symbol_count": int(len(issue_symbols)),
        "issue_symbols": issue_symbols,
        "issue_symbol_sample": issue_symbols[:10],
        "status_counts": df["status"].value_counts(dropna=False).to_dict(),
        "stage_status_counts": (
            df.groupby(["stage", "status"], dropna=False).size().reset_index(name="count").to_dict(orient="records")
            if "stage" in df.columns
            else []
        ),
        "stage_reason_counts": (
            df.groupby(["stage", "status", "reason"], dropna=False).size().reset_index(name="count").to_dict(orient="records")
            if {"stage", "status", "reason"}.issubset(df.columns)
            else []
        ),
        "issue_rows": issue_rows,
        "samples": samples,
    }


def _summarize_technical_input_blockers(
    *,
    refresh_status: dict[str, Any],
    missing_symbols: list[str] | None = None,
) -> dict[str, Any]:
    rows = [row for row in (refresh_status.get("issue_rows") or []) if isinstance(row, dict)]
    hard_reasons = {"dhan_daily_sync_failed_no_history", "ohlcv_sync_zero_rows", "ohlcv_no_new_data"}
    stale_reasons = {"dhan_daily_sync_failed_with_stale_history"}
    hard_symbols: set[str] = set()
    stale_symbols: set[str] = set()
    reason_counts: Counter[str] = Counter()
    symbol_reasons: dict[str, set[str]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        stage = str(row.get("stage") or "").strip().lower()
        reason = str(row.get("reason") or "").strip()
        if not symbol or not reason:
            continue
        reason_counts[reason] += 1
        symbol_reasons.setdefault(symbol, set()).add(reason)
        if stage == "ohlcv" and reason in hard_reasons:
            hard_symbols.add(symbol)
        elif stage == "ohlcv" and reason in stale_reasons:
            stale_symbols.add(symbol)
    missing_set = {str(symbol or "").strip().upper() for symbol in (missing_symbols or []) if str(symbol or "").strip()}
    hard_symbols |= {
        symbol
        for symbol in missing_set
        if symbol_reasons.get(symbol, set()) & hard_reasons
    }
    actionable_missing_symbols = sorted(missing_set - hard_symbols)
    return {
        "status": "has_hard_input_blockers" if hard_symbols else "has_partial_stale_history_blockers" if stale_symbols else "no_known_input_blockers",
        "hard_blocker_symbol_count": int(len(hard_symbols)),
        "hard_blocker_symbols": sorted(hard_symbols),
        "hard_blocker_symbol_sample": sorted(hard_symbols)[:10],
        "stale_history_symbol_count": int(len(stale_symbols)),
        "stale_history_symbols": sorted(stale_symbols),
        "stale_history_symbol_sample": sorted(stale_symbols)[:10],
        "actionable_missing_symbol_count": int(len(actionable_missing_symbols)),
        "actionable_missing_symbols": actionable_missing_symbols,
        "reason_counts": dict(reason_counts),
        "policy": {
            "hard_blocker_action": "suppress_context_watch_until_ohlcv_history_exists_or_symbol_is_excluded",
            "stale_history_action": "keep_visible_as_partial_blocker_do_not_treat_as_fresh",
            "authority_scope": "diagnostic_policy_only_no_portfolio_no_broker",
            "broker_execution_allowed": False,
        },
    }


def _load_dhan_daily_availability(symbols: list[str], asof_date: pd.Timestamp) -> dict[str, Any]:
    normalized_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    if not normalized_symbols:
        return {"status": "no_symbols", "covered_symbol_count": 0, "missing_symbol_count": 0}
    if not _table_exists(DHAN_OHLCV_DAILY_TABLE):
        return {
            "status": "dhan_daily_table_missing",
            "covered_symbol_count": 0,
            "missing_symbol_count": int(len(normalized_symbols)),
            "operator_action": f"{DHAN_OHLCV_DAILY_TABLE} does not exist; run Dhan OHLCV download before technical refresh.",
        }
    try:
        df = sql_to_df(
            f"""
            SELECT
                UPPER(TRIM(ticker)) AS symbol,
                MAX(date) AS latest_date,
                COUNT(*) AS row_count
            FROM {DHAN_OHLCV_DAILY_TABLE}
            WHERE date <= %s
              AND UPPER(TRIM(ticker)) = ANY(%s)
              AND LOWER(TRIM(COALESCE(asset_type, 'stock'))) = 'stock'
            GROUP BY UPPER(TRIM(ticker))
            """,
            params=(asof_date, normalized_symbols),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_dhan_daily_availability_failed",
            source=DHAN_OHLCV_DAILY_TABLE,
            severity="warn",
            reason="Recommendation diagnostics could not load Dhan daily OHLCV availability for context-watch symbols.",
            error=exc,
            metadata={"asof_date": asof_date.isoformat(), "symbol_count": len(normalized_symbols)},
        )
        return {
            "status": "dhan_daily_availability_load_failed",
            "covered_symbol_count": 0,
            "missing_symbol_count": int(len(normalized_symbols)),
            "error_type": exc.__class__.__name__,
        }
    if df.empty:
        return {
            "status": "dhan_daily_empty_for_symbols",
            "covered_symbol_count": 0,
            "missing_symbol_count": int(len(normalized_symbols)),
            "missing_symbol_sample": normalized_symbols[:10],
        }
    working = df.copy()
    working["symbol"] = working["symbol"].astype("string").str.strip().str.upper()
    working["latest_date"] = pd.to_datetime(working["latest_date"], utc=True, errors="coerce").dt.normalize()
    covered_symbols = sorted(set(working["symbol"].dropna().tolist()))
    missing_symbols = sorted(set(normalized_symbols) - set(covered_symbols))
    latest_dates = working["latest_date"].dropna()
    latest_available_date = None if latest_dates.empty else latest_dates.max().normalize()
    stale_to_asof_days = None
    if latest_available_date is not None:
        stale_to_asof_days = int((asof_date.normalize() - latest_available_date).days)
    lagging_samples = []
    if latest_available_date is not None:
        lagging = working.loc[working["latest_date"].lt(latest_available_date)].copy()
        for row in lagging.sort_values(["latest_date", "symbol"], ascending=[True, True]).head(10).to_dict(orient="records"):
            lagging_samples.append(
                {
                    "symbol": _text(row.get("symbol")),
                    "latest_date": None if pd.isna(row.get("latest_date")) else pd.Timestamp(row.get("latest_date")).isoformat(),
                    "row_count": _num(row.get("row_count")),
                }
            )
    status = "dhan_daily_current"
    if latest_available_date is None:
        status = "dhan_daily_dates_missing"
    elif stale_to_asof_days is not None and stale_to_asof_days > 0:
        status = "dhan_daily_lagging_asof"
    if missing_symbols:
        status = "dhan_daily_partial" if covered_symbols else "dhan_daily_empty_for_symbols"
    return {
        "status": status,
        "latest_available_date": None if latest_available_date is None else latest_available_date.isoformat(),
        "stale_to_asof_days": stale_to_asof_days,
        "covered_symbol_count": int(len(covered_symbols)),
        "missing_symbol_count": int(len(missing_symbols)),
        "missing_symbol_sample": missing_symbols[:10],
        "lagging_symbol_sample": lagging_samples,
    }


def summarize_market_participation_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {
            "status": "not_loaded",
            "market_participation_label": "unknown",
            "operator_action": "Market participation context was not loaded for this in-memory diagnostic call.",
            "policy_boundary": {
                "report_mode": "read_only_diagnostic",
                "portfolio_authority": "none",
                "broker_execution_allowed": False,
                "decision_use": "attribute missing BUY rows against broad benchmark participation; do not trade from this context alone",
            },
        }
    explicit_status = str(context.get("status") or "").strip()
    if explicit_status.startswith("market_participation_") and isinstance(context.get("policy_boundary"), dict):
        return context
    ret20 = _num(context.get("benchmark_ret_20d"))
    ret60 = _num(context.get("benchmark_ret_60d"))
    if ret20 is None and ret60 is None:
        label = "unknown"
        status = explicit_status if explicit_status.startswith("market_participation_") else "market_participation_unknown"
        operator_action = str(
            context.get("operator_action")
            or "Benchmark participation is unavailable; do not infer rising-market underparticipation from memory or headlines."
        )
    elif (ret20 is not None and ret20 >= 0.03) or (ret60 is not None and ret60 >= 0.06):
        label = "benchmark_uptrend"
        status = "market_participation_benchmark_uptrend"
        operator_action = (
            "Benchmark participation is positive; if BUY rows are absent, diagnose technical trigger strictness, stale data, "
            "context-overlay conversion, and downstream consolidation before changing global regime policy."
        )
    elif (ret20 is not None and ret20 <= -0.03) or (ret60 is not None and ret60 <= -0.06):
        label = "benchmark_downtrend"
        status = "market_participation_benchmark_downtrend"
        operator_action = "Benchmark participation is weak; missing BUY rows may be consistent with broad risk control, but still inspect sector/symbol context separately."
    else:
        label = "benchmark_flat_or_mixed"
        status = "market_participation_flat_or_mixed"
        operator_action = "Benchmark participation is flat or mixed; diagnose missing BUY rows from symbol-level technical/context evidence rather than a single regime label."
    return {
        "status": status,
        "market_participation_label": label,
        "asof_date": None if context.get("asof_date") is None else str(context.get("asof_date")),
        "benchmark_name": context.get("benchmark_name"),
        "benchmark_close": _num(context.get("benchmark_close")),
        "benchmark_ret_20d": ret20,
        "benchmark_ret_60d": ret60,
        "regime_name": context.get("regime_name"),
        "macro_risk_state": context.get("macro_risk_state"),
        "risk_off_flag": bool(_boolish(context.get("risk_off_flag"))),
        "load_ts": None if context.get("load_ts") is None else str(context.get("load_ts")),
        "operator_action": operator_action,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "attribute missing BUY rows against broad benchmark participation; do not trade from this context alone",
        },
    }


def load_market_participation_context(*, asof_date: pd.Timestamp | None = None) -> dict[str, Any]:
    try:
        if not _table_exists(MARKET_REGIME_TABLE):
            return summarize_market_participation_context(
                {
                    "status": "market_participation_unavailable",
                    "market_participation_label": "unknown",
                    "operator_action": "Market regime table is unavailable; run complete_data.sh/all_advisory.sh before interpreting rising-market participation.",
                }
            )
        effective_asof = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date is not None else None
        where_clause = "WHERE asof_date <= %s" if effective_asof is not None and not pd.isna(effective_asof) else ""
        params = (effective_asof,) if where_clause else None
        df = sql_to_df(
            f"""
            SELECT
                asof_date,
                benchmark_name,
                benchmark_close,
                benchmark_ret_20d,
                benchmark_ret_60d,
                macro_risk_state,
                risk_off_flag,
                regime_name,
                load_ts
            FROM {MARKET_REGIME_TABLE}
            {where_clause}
            ORDER BY asof_date DESC
            LIMIT 1
            """,
            params=params,
        )
        if df.empty:
            return summarize_market_participation_context(
                {
                    "status": "market_participation_no_rows",
                    "market_participation_label": "unknown",
                    "operator_action": "No market participation rows were found; refresh regime/benchmark snapshots before interpreting missing BUYs.",
                }
            )
        return summarize_market_participation_context(df.iloc[0].to_dict())
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_market_participation_load_failed",
            source=MARKET_REGIME_TABLE,
            severity="warn",
            reason="Recommendation diagnostics could not load broad benchmark participation context.",
            error=exc,
            metadata={"asof_date": None if asof_date is None else str(asof_date)},
        )
        return summarize_market_participation_context(
            {
                "status": "market_participation_unavailable",
                "market_participation_label": "unknown",
                "operator_action": "Market participation context could not be loaded; do not infer benchmark-up underparticipation from memory.",
            }
        )


def load_recommendation_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 500) -> pd.DataFrame:
    effective_asof = asof_date or _latest_asof_date()
    if effective_asof is None:
        return pd.DataFrame()
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            published_on,
            symbol,
            setup_id,
            action_code,
            action_source,
            source_action,
            transaction_type,
            execution_mode,
            action_fraction,
            approved_allocation_inr,
            reference_price,
            stop_price,
            recommended_target_price,
            expected_horizon_days,
            invest_score_pct,
            action_reason,
            action_detail,
            recommendation_reason_json,
            reason_contract_status,
            feature_freshness_json,
            raw_context_json,
            load_ts
        FROM {ACTION_RECOMMENDATIONS_TABLE}
        WHERE asof_date = %s
        ORDER BY
            CASE
                WHEN action_code IN ('BUY', 'BUY_MORE') THEN 0
                WHEN action_code = 'MANUAL_REVIEW' THEN 1
                WHEN action_code = 'WATCH' THEN 2
                ELSE 3
            END,
            invest_score_pct DESC NULLS LAST,
            symbol
        LIMIT %s
        """,
        params=(effective_asof, int(limit)),
    )


def load_allocation_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 1000) -> pd.DataFrame:
    effective_asof = asof_date or _latest_asof_date()
    columns = _table_columns(ALLOCATIONS_TABLE)
    if effective_asof is None or not columns:
        return pd.DataFrame()
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            symbol,
            setup_id,
            unique_id,
            {_optional_select("allocation_status", columns)} ,
            {_optional_select("suggested_allocation_inr", columns, cast="DOUBLE PRECISION")} ,
            {_optional_select("candidate_state", columns)} ,
            {_optional_select("current_state", columns)} ,
            {_optional_select("is_base_candidate_fallback", columns, cast="BOOLEAN")} ,
            {_optional_select("technical_state", columns)} ,
            {_optional_select("technical_trigger_type", columns)} ,
            {_optional_select("technical_entry_confirmed", columns, cast="BOOLEAN")} ,
            {_optional_select("notes", columns)} ,
            load_ts
        FROM {ALLOCATIONS_TABLE}
        WHERE asof_date = %s
        ORDER BY allocation_status NULLS LAST, symbol
        LIMIT %s
        """,
        params=(effective_asof, int(limit)),
    )


def load_portfolio_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 1000) -> pd.DataFrame:
    effective_asof = asof_date or _latest_asof_date()
    columns = _table_columns(PORTFOLIO_TABLE)
    if effective_asof is None or not columns:
        return pd.DataFrame()
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            symbol,
            setup_id,
            unique_id,
            {_optional_select("portfolio_status", columns)} ,
            {_optional_select("portfolio_reason", columns)} ,
            {_optional_select("recommended_action", columns)} ,
            {_optional_select("approved_allocation_inr", columns, cast="DOUBLE PRECISION")} ,
            {_optional_select("candidate_state", columns)} ,
            {_optional_select("current_state", columns)} ,
            {_optional_select("is_base_candidate_fallback", columns, cast="BOOLEAN")} ,
            {_optional_select("technical_state", columns)} ,
            {_optional_select("technical_trigger_type", columns)} ,
            {_optional_select("technical_entry_confirmed", columns, cast="BOOLEAN")} ,
            load_ts
        FROM {PORTFOLIO_TABLE}
        WHERE asof_date = %s
        ORDER BY portfolio_status NULLS LAST, portfolio_reason NULLS LAST, symbol
        LIMIT %s
        """,
        params=(effective_asof, int(limit)),
    )


def load_candidate_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 1000) -> pd.DataFrame:
    effective_asof = asof_date or _latest_asof_date()
    columns = _table_columns(CANDIDATES_TABLE)
    if effective_asof is None or not columns:
        return pd.DataFrame()
    technical_columns = _table_columns(TECHNICAL_DAILY_TABLE)
    technical_feature_columns = {
        "adj_close": "DOUBLE PRECISION",
        "avg_traded_value_20d": "DOUBLE PRECISION",
        "median_volume_20d": "DOUBLE PRECISION",
        "atr_pct": "DOUBLE PRECISION",
        "gap_frequency_60d": "DOUBLE PRECISION",
        "base_depth_60d_pct": "DOUBLE PRECISION",
        "pass_liquidity_20d": "BOOLEAN",
        "pass_gap_behavior": "BOOLEAN",
        "pass_above_dma_20": "BOOLEAN",
        "pass_above_dma_50": "BOOLEAN",
        "pass_above_dma_150": "BOOLEAN",
        "pass_above_dma_200": "BOOLEAN",
        "pass_trend_alignment": "BOOLEAN",
        "dma_50_slope_20d_pct": "DOUBLE PRECISION",
        "dma_150_slope_20d_pct": "DOUBLE PRECISION",
        "dist_52w_high": "DOUBLE PRECISION",
        "trend_persistence_60d": "DOUBLE PRECISION",
        "trend_persistence_120d": "DOUBLE PRECISION",
        "higher_high_count_20d": "DOUBLE PRECISION",
        "higher_low_count_20d": "DOUBLE PRECISION",
        "pivot_distance_20d_pct": "DOUBLE PRECISION",
        "range_contraction_ratio": "DOUBLE PRECISION",
        "volatility_contraction_flag": "BOOLEAN",
        "tight_close_upper_half_20d": "DOUBLE PRECISION",
        "support_hold_rate_20d": "DOUBLE PRECISION",
        "breakout_extension_pct": "DOUBLE PRECISION",
        "bb_width_rank_252d": "DOUBLE PRECISION",
        "breakout_day_volume_vs_20d": "DOUBLE PRECISION",
        "up_down_volume_ratio_20d": "DOUBLE PRECISION",
        "accumulation_days_20d": "DOUBLE PRECISION",
        "distribution_days_20d": "DOUBLE PRECISION",
        "pullback_volume_dryup_ratio_20d": "DOUBLE PRECISION",
        "rs_vs_benchmark": "DOUBLE PRECISION",
        "rs_vs_sector": "DOUBLE PRECISION",
        "stock_ret_60d": "DOUBLE PRECISION",
        "stock_ret_120d": "DOUBLE PRECISION",
        "support_distance_20d_pct": "DOUBLE PRECISION",
        "close_location_pct": "DOUBLE PRECISION",
        "dist_20d_high": "DOUBLE PRECISION",
    }
    technical_selects = [
        _optional_select_alias("td", column, technical_columns, cast=cast)
        if technical_columns
        else f"NULL::{cast} AS {column}"
        for column, cast in technical_feature_columns.items()
    ]
    if technical_columns:
        technical_series_filter = (
            "AND UPPER(TRIM(COALESCE(td.series, 'daily'))) IN ('DAILY', 'EQ')"
            if "series" in technical_columns
            else ""
        )
        technical_order_by = (
            "td.asof_date DESC, td.load_ts DESC NULLS LAST"
            if "load_ts" in technical_columns
            else "td.asof_date DESC"
        )
        technical_join = f"""
        LEFT JOIN LATERAL (
            SELECT *
            FROM {TECHNICAL_DAILY_TABLE} td
            WHERE td.asof_date <= c.asof_date
              AND UPPER(TRIM(td.symbol)) = UPPER(TRIM(c.symbol))
              {technical_series_filter}
            ORDER BY {technical_order_by}
            LIMIT 1
        ) td ON TRUE
        """
        technical_meta_selects = [
            "td.asof_date AS technical_feature_asof_date",
            "td.load_ts AS technical_feature_load_ts" if "load_ts" in technical_columns else "NULL::TIMESTAMPTZ AS technical_feature_load_ts",
        ]
    else:
        technical_join = ""
        technical_meta_selects = [
            "NULL::DATE AS technical_feature_asof_date",
            "NULL::TIMESTAMPTZ AS technical_feature_load_ts",
        ]
    return sql_to_df(
        f"""
        SELECT
            c.asof_date,
            c.symbol,
            c.setup_id,
            c.setup_name,
            c.screener_slug,
            c.source_screener_slug,
            c.rule_pass,
            c.watch_enabled,
            c.candidate_state,
            c.technical_state,
            c.technical_trigger_type,
            {_optional_select_alias("c", "technical_entry_confirmed", columns, cast="BOOLEAN")} ,
            {_optional_select_alias("c", "technical_setup_archetype", columns)} ,
            {_optional_select_alias("c", "technical_setup_quality_json", columns)} ,
            c.technical_score,
            c.setup_score,
            c.regime_name,
            c.base_regime,
            c.watch_reason_detail,
            c.watch_reasons,
            c.load_ts,
            {", ".join(technical_meta_selects)},
            {", ".join(technical_selects)}
        FROM {CANDIDATES_TABLE} c
        {technical_join}
        WHERE c.asof_date = %s
        ORDER BY c.setup_score DESC NULLS LAST, c.technical_score DESC NULLS LAST, c.symbol
        LIMIT %s
        """,
        params=(effective_asof, int(limit)),
    )


def load_latest_trading_day_on_or_before(asof_date: pd.Timestamp | None) -> pd.Timestamp | None:
    requested = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date is not None else None
    requested = None if requested is None or pd.isna(requested) else requested.normalize()
    if requested is None or not _table_exists(TRADING_DAYS_TABLE):
        return None
    try:
        df = sql_to_df(
            f"""
            SELECT MAX(date) AS latest_trading_date
            FROM {TRADING_DAYS_TABLE}
            WHERE date <= %s
            """,
            params=(requested,),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_trading_day_lookup_failed",
            source=TRADING_DAYS_TABLE,
            severity="warn",
            reason="Recommendation diagnostics could not load the latest trading day for candidate-source availability.",
            error=exc,
            metadata={"asof_date": requested.isoformat()},
        )
        return None
    if df.empty:
        return None
    latest = pd.to_datetime(df.iloc[0].get("latest_trading_date"), utc=True, errors="coerce")
    return None if pd.isna(latest) else latest.normalize()


def load_trading_day_gap(*, from_date: pd.Timestamp | None, to_date: pd.Timestamp | None) -> int | None:
    start = pd.to_datetime(from_date, utc=True, errors="coerce") if from_date is not None else None
    end = pd.to_datetime(to_date, utc=True, errors="coerce") if to_date is not None else None
    start = None if start is None or pd.isna(start) else start.normalize()
    end = None if end is None or pd.isna(end) else end.normalize()
    if start is None or end is None or start >= end or not _table_exists(TRADING_DAYS_TABLE):
        return 0 if start is not None and end is not None and start == end else None
    try:
        df = sql_to_df(
            f"""
            SELECT COUNT(*)::BIGINT AS trading_day_gap
            FROM {TRADING_DAYS_TABLE}
            WHERE date > %s
              AND date <= %s
            """,
            params=(start, end),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_trading_day_gap_lookup_failed",
            source=TRADING_DAYS_TABLE,
            severity="warn",
            reason="Recommendation diagnostics could not compute trading-day candidate staleness.",
            error=exc,
            metadata={"from_date": start.isoformat(), "to_date": end.isoformat()},
        )
        return None
    if df.empty:
        return None
    return int(df.iloc[0].get("trading_day_gap") or 0)


def load_candidate_source_availability(*, asof_date: pd.Timestamp | None = None) -> dict[str, Any]:
    effective_asof = asof_date or _latest_asof_date()
    if effective_asof is None or not _table_exists(CANDIDATES_TABLE):
        return {
            "status": "candidate_source_unavailable",
            "requested_asof_date": None if effective_asof is None else pd.to_datetime(effective_asof, utc=True).normalize().isoformat(),
            "candidate_rows_for_asof": 0,
            "latest_candidate_asof_date": None,
            "latest_candidate_rows": 0,
            "operator_action": "Candidate source availability could not be checked; verify advisory_candidates exists and rerun all_advisory.sh.",
        }
    requested = pd.to_datetime(effective_asof, utc=True, errors="coerce")
    requested = None if pd.isna(requested) else requested.normalize()
    try:
        df = sql_to_df(
            f"""
            WITH requested AS (
                SELECT COUNT(*)::BIGINT AS candidate_rows_for_asof
                FROM {CANDIDATES_TABLE}
                WHERE asof_date = %s
            ),
            latest_day AS (
                SELECT MAX(asof_date) AS latest_candidate_asof_date
                FROM {CANDIDATES_TABLE}
            ),
            latest AS (
                SELECT COUNT(*)::BIGINT AS latest_candidate_rows
                FROM {CANDIDATES_TABLE}
                WHERE asof_date = (SELECT latest_candidate_asof_date FROM latest_day)
            )
            SELECT
                requested.candidate_rows_for_asof,
                latest_day.latest_candidate_asof_date,
                latest.latest_candidate_rows
            FROM requested
            CROSS JOIN latest_day
            CROSS JOIN latest
            """,
            params=(requested,),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_candidate_source_availability_failed",
            source=CANDIDATES_TABLE,
            severity="warn",
            reason="Recommendation diagnostics could not inspect candidate source availability.",
            error=exc,
            metadata={"asof_date": None if requested is None else requested.isoformat()},
        )
        return {
            "status": "candidate_source_availability_failed",
            "requested_asof_date": None if requested is None else requested.isoformat(),
            "candidate_rows_for_asof": 0,
            "latest_candidate_asof_date": None,
            "latest_candidate_rows": 0,
            "operator_action": "Candidate source availability lookup failed; rerun recommendation diagnostics after checking database health.",
            "error_type": exc.__class__.__name__,
        }
    if df.empty:
        latest_asof = None
        candidate_rows_for_asof = 0
        latest_candidate_rows = 0
    else:
        latest_asof = pd.to_datetime(df.iloc[0].get("latest_candidate_asof_date"), utc=True, errors="coerce")
        latest_asof = None if pd.isna(latest_asof) else latest_asof.normalize()
        candidate_rows_for_asof = int(df.iloc[0].get("candidate_rows_for_asof") or 0)
        latest_candidate_rows = int(df.iloc[0].get("latest_candidate_rows") or 0)
    date_gap_days = None
    latest_candidate_relation = "no_latest_candidate_rows"
    latest_candidate_age_days = None
    if requested is not None and latest_asof is not None:
        date_gap_days = int((latest_asof - requested).days)
        latest_candidate_age_days = int((requested - latest_asof).days)
        latest_candidate_relation = (
            "same_date"
            if latest_candidate_age_days == 0
            else "latest_candidate_older_than_requested"
            if latest_candidate_age_days > 0
            else "latest_candidate_newer_than_requested"
        )
    latest_trading_asof = load_latest_trading_day_on_or_before(requested)
    latest_candidate_matches_latest_trading_day = bool(
        requested is not None
        and latest_asof is not None
        and latest_trading_asof is not None
        and latest_asof == latest_trading_asof
    )
    latest_candidate_trading_age_days = load_trading_day_gap(from_date=latest_asof, to_date=latest_trading_asof)
    candidate_lookup_asof = requested
    status = (
        "candidate_rows_available_for_asof"
        if candidate_rows_for_asof > 0
        else "candidate_rows_current_for_latest_trading_day"
        if latest_candidate_matches_latest_trading_day and latest_candidate_rows > 0
        else "candidate_rows_missing_for_asof_latest_exists"
        if latest_asof is not None and latest_candidate_rows > 0
        else "candidate_rows_missing"
    )
    if status == "candidate_rows_current_for_latest_trading_day":
        candidate_lookup_asof = latest_asof
    return {
        "status": status,
        "requested_asof_date": None if requested is None else requested.isoformat(),
        "candidate_lookup_asof_date": None if candidate_lookup_asof is None else candidate_lookup_asof.isoformat(),
        "candidate_rows_for_asof": candidate_rows_for_asof,
        "latest_candidate_asof_date": None if latest_asof is None else latest_asof.isoformat(),
        "latest_candidate_rows": latest_candidate_rows,
        "latest_trading_asof_date": None if latest_trading_asof is None else latest_trading_asof.isoformat(),
        "latest_candidate_matches_latest_trading_day": latest_candidate_matches_latest_trading_day,
        "latest_candidate_trading_age_days": latest_candidate_trading_age_days,
        "date_gap_days": date_gap_days,
        "latest_candidate_relation": latest_candidate_relation,
        "latest_candidate_age_days": latest_candidate_age_days,
        "operator_action": (
            "Candidate rows exist for the diagnostic date."
            if status == "candidate_rows_available_for_asof"
            else "No candidate rows exist for this calendar date, but the latest candidate rows match the latest trading day; use the candidate lookup date before treating missing BUYs as stale candidate evidence."
            if status == "candidate_rows_current_for_latest_trading_day"
            else "No candidate rows exist for this diagnostic date, and the latest candidate rows are older than the requested date; run all_advisory.sh for the relevant trading date before interpreting no-BUY causes."
            if latest_candidate_relation == "latest_candidate_older_than_requested"
            else "No candidate rows exist for this diagnostic date, but another candidate date exists; run all_advisory.sh for the relevant trading date before interpreting no-BUY causes."
            if status == "candidate_rows_missing_for_asof_latest_exists"
            else "No candidate rows exist; run all_advisory.sh and verify rule-engine candidate persistence."
        ),
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "explain whether upstream candidate evidence exists for the diagnostic action date",
        },
    }


def load_rejection_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 1000) -> pd.DataFrame:
    effective_asof = asof_date or _latest_asof_date()
    if effective_asof is None or not _table_exists(REJECTIONS_TABLE):
        return pd.DataFrame()
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            symbol,
            setup_id,
            reason_code,
            reason_detail,
            severity,
            is_near_miss,
            delta_to_pass,
            base_regime,
            news_overlay,
            load_ts
        FROM {REJECTIONS_TABLE}
        WHERE asof_date = %s
        ORDER BY severity DESC NULLS LAST, delta_to_pass ASC NULLS LAST, symbol
        LIMIT %s
        """,
        params=(effective_asof, int(limit)),
    )


TECHNICAL_RECONSTRUCTION_FEATURE_COLUMNS = {
    "adj_close",
    "avg_traded_value_20d",
    "pivot_distance_20d_pct",
    "breakout_day_volume_vs_20d",
    "support_distance_20d_pct",
    "support_hold_rate_20d",
    "pullback_volume_dryup_ratio_20d",
    "close_location_pct",
}


def _reconstruct_technical_setup_quality(row: dict[str, Any]) -> dict[str, Any]:
    """Rebuild explain-only technical blocker contracts when older candidate rows lack JSON."""
    if not any(_num(row.get(column)) is not None for column in TECHNICAL_RECONSTRUCTION_FEATURE_COLUMNS):
        return {}
    try:
        reconstructed = evaluate_pre_entry_state(pd.Series(row))
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_technical_contract_reconstruction_failed",
            source=TECHNICAL_DAILY_TABLE,
            severity="warn",
            reason="Recommendation diagnostics could not reconstruct technical trigger/readiness contracts from point-in-time technical features.",
            error=exc,
            metadata={
                "symbol": str(row.get("symbol") or "").strip().upper(),
                "setup_id": row.get("setup_id"),
                "asof_date": str(row.get("asof_date")),
            },
        )
        return {}
    setup_quality = reconstructed.get("technical_setup_quality") if isinstance(reconstructed, dict) else None
    if not isinstance(setup_quality, dict):
        return {}
    out = dict(setup_quality)
    out["diagnostic_reconstructed"] = True
    out["diagnostic_reconstruction_source"] = TECHNICAL_DAILY_TABLE
    out["diagnostic_reconstruction_asof_date"] = _json_default(row.get("technical_feature_asof_date"))
    out["diagnostic_reconstruction_note"] = (
        "Rebuilt by recommendation diagnostics from point-in-time technical features because the persisted candidate row lacked a trigger/readiness contract."
    )
    return out


def summarize_upstream_candidates(
    candidates: pd.DataFrame,
    rejections: pd.DataFrame | None = None,
    action_rows: pd.DataFrame | None = None,
) -> dict[str, Any]:
    rejections = rejections if isinstance(rejections, pd.DataFrame) else pd.DataFrame()
    if candidates.empty and rejections.empty:
        return {
            "candidate_rows": 0,
            "rejection_rows": 0,
            "diagnosis": "no_upstream_candidate_or_rejection_rows_loaded",
            "candidate_state_counts": {},
            "technical_state_counts": {},
            "rejection_reason_counts": {},
        }
    candidate_records = candidates.to_dict(orient="records") if not candidates.empty else []
    state_counts = Counter(str(row.get("candidate_state") or "missing").strip().upper() for row in candidate_records)
    technical_counts = Counter(str(row.get("technical_state") or "missing").strip().upper() for row in candidate_records)
    trigger_counts = Counter(str(row.get("technical_trigger_type") or "missing").strip().upper() for row in candidate_records)
    rule_pass_count = sum(1 for row in candidate_records if bool(row.get("rule_pass")))
    watch_enabled_count = sum(1 for row in candidate_records if bool(row.get("watch_enabled")))
    positive_candidate_states = {"PASS_NOW", "READY", "BUY_TRIGGERED", "ADD_ON_PULLBACK", "HOLD"}
    constructive_technical_states = {"READY", "BUY_TRIGGERED", "NEAR_PIVOT", "ADD_ON_PULLBACK", "HOLD"}
    action_records = action_rows.to_dict(orient="records") if isinstance(action_rows, pd.DataFrame) and not action_rows.empty else []
    final_action_by_symbol = {
        str(row.get("symbol") or "").strip().upper(): {
            "action_code": str(row.get("action_code") or "").strip().upper(),
            "action_source": row.get("action_source"),
            "execution_mode": row.get("execution_mode"),
            "reason_contract_status": row.get("reason_contract_status"),
        }
        for row in action_records
        if str(row.get("symbol") or "").strip()
    }
    positive_candidate_rows = [
        row
        for row in candidate_records
        if str(row.get("candidate_state") or "").strip().upper() in positive_candidate_states
        or str(row.get("technical_state") or "").strip().upper() in constructive_technical_states
        or bool(row.get("rule_pass"))
    ]
    positive_candidate_rows.sort(
        key=lambda row: (
            _num(row.get("setup_score")) or -999.0,
            _num(row.get("technical_score")) or -999.0,
        ),
        reverse=True,
    )
    pass_now_with_technical_ignore = [
        row
        for row in positive_candidate_rows
        if str(row.get("candidate_state") or "").strip().upper() == "PASS_NOW"
        and str(row.get("technical_state") or "").strip().upper() == "IGNORE"
    ]
    technical_entry_confirmed_states = {"BUY_TRIGGERED", "ADD_ON_PULLBACK"}
    technical_watch_states = {"WATCHLIST", "NEAR_PIVOT", "READY", "HOLD"}
    technical_ignore_states = {"IGNORE", "REJECT", "NO_BUY"}
    technical_confirmed_rows = [
        row
        for row in positive_candidate_rows
        if str(row.get("technical_state") or "").strip().upper() in technical_entry_confirmed_states
        or str(row.get("technical_entry_confirmed") or "").strip().lower() in {"1", "true", "yes"}
    ]
    technical_watch_rows = [
        row
        for row in positive_candidate_rows
        if str(row.get("technical_state") or "").strip().upper() in technical_watch_states
    ]
    technical_ignore_rows = [
        row
        for row in positive_candidate_rows
        if str(row.get("technical_state") or "").strip().upper() in technical_ignore_states
    ]
    soft_failure_counts: Counter[str] = Counter()
    nearest_trigger_counts: Counter[str] = Counter()
    trigger_blocker_counts: Counter[str] = Counter()
    trigger_blocker_archetype_counts: Counter[str] = Counter()
    trigger_blocker_metric_counts: Counter[str] = Counter()
    trigger_blocker_contract_count = 0
    trigger_blocker_reconstructed_count = 0
    trigger_blocker_samples: list[dict[str, Any]] = []
    buy_readiness_blocker_counts: Counter[str] = Counter()
    buy_readiness_category_counts: Counter[str] = Counter()
    buy_readiness_metric_counts: Counter[str] = Counter()
    buy_readiness_contract_count = 0
    buy_readiness_reconstructed_count = 0
    buy_readiness_samples: list[dict[str, Any]] = []
    for row in positive_candidate_rows:
        values = _jsonish(row.get("soft_failures_json"), [])
        if not isinstance(values, list):
            values = [values]
        for value in values:
            text = str(value or "").strip()
            if text:
                soft_failure_counts[text] += 1
        setup_quality = _jsonish(row.get("technical_setup_quality_json"), {})
        if not isinstance(setup_quality, dict):
            setup_quality = {}
        if not any(
            key in setup_quality
            for key in {
                "entry_trigger_status",
                "nearest_entry_trigger_type",
                "entry_trigger_blockers",
                "entry_trigger_diagnostics",
                "buy_readiness",
            }
        ):
            reconstructed_quality = _reconstruct_technical_setup_quality(row)
            if reconstructed_quality:
                setup_quality = reconstructed_quality
        trigger_diagnostics = setup_quality.get("entry_trigger_diagnostics")
        if not isinstance(trigger_diagnostics, dict):
            trigger_diagnostics = {}
        has_trigger_blocker_contract = any(
            key in setup_quality
            for key in {
                "entry_trigger_status",
                "nearest_entry_trigger_type",
                "entry_trigger_blockers",
                "entry_trigger_diagnostics",
            }
        ) or bool(trigger_diagnostics)
        nearest_trigger = str(
            setup_quality.get("nearest_entry_trigger_type")
            or trigger_diagnostics.get("nearest_trigger_type")
            or row.get("technical_trigger_type")
            or "missing"
        ).strip().lower()
        if has_trigger_blocker_contract and nearest_trigger:
            nearest_trigger_counts[nearest_trigger] += 1
        blockers = setup_quality.get("entry_trigger_blockers")
        if not isinstance(blockers, list):
            blockers = trigger_diagnostics.get("nearest_trigger_blockers")
        if not isinstance(blockers, list):
            blockers = []
        if has_trigger_blocker_contract:
            trigger_blocker_contract_count += 1
            if setup_quality.get("diagnostic_reconstructed") is True:
                trigger_blocker_reconstructed_count += 1
        for blocker in blockers:
            if not isinstance(blocker, dict):
                continue
            code = str(blocker.get("code") or "missing").strip()
            archetype = str(blocker.get("archetype") or nearest_trigger or "missing").strip()
            metric = str(blocker.get("metric") or "missing").strip()
            if code:
                trigger_blocker_counts[code] += 1
            if archetype:
                trigger_blocker_archetype_counts[archetype] += 1
            if metric:
                trigger_blocker_metric_counts[metric] += 1
        if blockers and len(trigger_blocker_samples) < 10:
            trigger_blocker_samples.append(
                {
                    "symbol": str(row.get("symbol") or "").strip().upper(),
                    "setup_id": row.get("setup_id"),
                    "candidate_state": row.get("candidate_state"),
                    "technical_state": row.get("technical_state"),
                    "technical_setup_archetype": row.get("technical_setup_archetype") or setup_quality.get("archetype"),
                    "nearest_trigger_type": nearest_trigger,
                    "diagnostic_reconstructed": bool(setup_quality.get("diagnostic_reconstructed")),
                    "diagnostic_reconstruction_asof_date": setup_quality.get("diagnostic_reconstruction_asof_date"),
                    "setup_score": _num(row.get("setup_score")),
                    "technical_score": _num(row.get("technical_score")),
                    "blockers": [
                        {
                            "code": item.get("code"),
                            "metric": item.get("metric"),
                            "current": item.get("current"),
                            "operator": item.get("operator"),
                            "threshold": item.get("threshold"),
                        }
                        for item in blockers[:5]
                        if isinstance(item, dict)
                    ],
                }
            )
        buy_readiness = setup_quality.get("buy_readiness")
        if isinstance(buy_readiness, dict):
            buy_readiness_contract_count += 1
            if setup_quality.get("diagnostic_reconstructed") is True:
                buy_readiness_reconstructed_count += 1
            buy_blockers = buy_readiness.get("blockers")
            if not isinstance(buy_blockers, list):
                buy_blockers = []
            for blocker in buy_blockers:
                if not isinstance(blocker, dict):
                    continue
                code = str(blocker.get("code") or "missing").strip()
                category = str(blocker.get("category") or "missing").strip()
                metric = str(blocker.get("metric") or "missing").strip()
                if code:
                    buy_readiness_blocker_counts[code] += 1
                if category:
                    buy_readiness_category_counts[category] += 1
                if metric and metric != "missing":
                    buy_readiness_metric_counts[metric] += 1
            if buy_blockers and len(buy_readiness_samples) < 10:
                buy_readiness_samples.append(
                    {
                        "symbol": str(row.get("symbol") or "").strip().upper(),
                        "setup_id": row.get("setup_id"),
                        "candidate_state": row.get("candidate_state"),
                        "technical_state": row.get("technical_state"),
                        "technical_trigger_type": row.get("technical_trigger_type"),
                        "diagnostic_reconstructed": bool(setup_quality.get("diagnostic_reconstructed")),
                        "diagnostic_reconstruction_asof_date": setup_quality.get("diagnostic_reconstruction_asof_date"),
                        "setup_score": _num(row.get("setup_score")),
                        "technical_score": _num(row.get("technical_score")),
                        "blockers": [
                            {
                                "code": item.get("code"),
                                "category": item.get("category"),
                                "metric": item.get("metric"),
                                "current": item.get("current"),
                                "operator": item.get("operator"),
                                "threshold": item.get("threshold"),
                                "gap_to_threshold": item.get("gap_to_threshold"),
                            }
                            for item in buy_blockers[:6]
                            if isinstance(item, dict)
                        ],
                    }
                )
    technical_trigger_blockers = {
        "status": (
            "trigger_blocker_contract_available"
            if trigger_blocker_contract_count
            else "trigger_blocker_contract_missing"
            if positive_candidate_rows
            else "no_constructive_candidates"
        ),
        "candidate_count_with_contract": int(trigger_blocker_contract_count),
        "candidate_count_without_contract": int(max(0, len(positive_candidate_rows) - trigger_blocker_contract_count)),
        "candidate_count_with_reconstructed_contract": int(trigger_blocker_reconstructed_count),
        "nearest_trigger_counts": dict(nearest_trigger_counts.most_common()),
        "blocker_counts": dict(trigger_blocker_counts.most_common()),
        "blocker_archetype_counts": dict(trigger_blocker_archetype_counts.most_common()),
        "blocker_metric_counts": dict(trigger_blocker_metric_counts.most_common()),
        "top_blockers": [
            {"code": code, "count": int(count)}
            for code, count in trigger_blocker_counts.most_common(10)
        ],
        "top_metrics": [
            {"metric": metric, "count": int(count)}
            for metric, count in trigger_blocker_metric_counts.most_common(10)
        ],
        "samples": trigger_blocker_samples,
        "authority_scope": "diagnostic_only",
        "action_policy_effect": "explain_only_no_threshold_change",
        "broker_execution_allowed": False,
    }
    buy_readiness_blockers = {
        "status": (
            "buy_readiness_contract_available"
            if buy_readiness_contract_count
            else "buy_readiness_contract_missing"
            if positive_candidate_rows
            else "no_constructive_candidates"
        ),
        "candidate_count_with_contract": int(buy_readiness_contract_count),
        "candidate_count_without_contract": int(max(0, len(positive_candidate_rows) - buy_readiness_contract_count)),
        "candidate_count_with_reconstructed_contract": int(buy_readiness_reconstructed_count),
        "blocker_counts": dict(buy_readiness_blocker_counts.most_common()),
        "category_counts": dict(buy_readiness_category_counts.most_common()),
        "metric_counts": dict(buy_readiness_metric_counts.most_common()),
        "top_blockers": [
            {"code": code, "count": int(count)}
            for code, count in buy_readiness_blocker_counts.most_common(10)
        ],
        "top_metrics": [
            {"metric": metric, "count": int(count)}
            for metric, count in buy_readiness_metric_counts.most_common(10)
        ],
        "samples": buy_readiness_samples,
        "authority_scope": "diagnostic_only",
        "action_policy_effect": "explain_only_no_threshold_change",
        "broker_execution_allowed": False,
    }
    archetype_stats: dict[str, dict[str, Any]] = {}
    for row in positive_candidate_rows:
        archetype = infer_calibration_setup_archetype(row)
        stats = archetype_stats.setdefault(
            archetype,
            {
                "technical_setup_archetype": archetype,
                "candidate_count": 0,
                "confirmed_entry_count": 0,
                "watch_or_near_entry_count": 0,
                "ignore_or_reject_count": 0,
                "rule_pass_count": 0,
                "final_action_counts": Counter(),
                "top_samples": [],
            },
        )
        stats["candidate_count"] += 1
        technical_state = str(row.get("technical_state") or "").strip().upper()
        if technical_state in technical_entry_confirmed_states or str(row.get("technical_entry_confirmed") or "").strip().lower() in {"1", "true", "yes"}:
            stats["confirmed_entry_count"] += 1
        if technical_state in technical_watch_states:
            stats["watch_or_near_entry_count"] += 1
        if technical_state in technical_ignore_states:
            stats["ignore_or_reject_count"] += 1
        if bool(row.get("rule_pass")):
            stats["rule_pass_count"] += 1
        final_action = (final_action_by_symbol.get(str(row.get("symbol") or "").strip().upper()) or {}).get("action_code") or "NO_FINAL_ACTION"
        stats["final_action_counts"][str(final_action).strip().upper()] += 1
        if len(stats["top_samples"]) < 5:
            stats["top_samples"].append(
                {
                    "symbol": str(row.get("symbol") or "").strip().upper(),
                    "setup_id": row.get("setup_id"),
                    "candidate_state": row.get("candidate_state"),
                    "technical_state": row.get("technical_state"),
                    "technical_trigger_type": row.get("technical_trigger_type"),
                    "setup_score": _num(row.get("setup_score")),
                    "technical_score": _num(row.get("technical_score")),
                    "final_action": final_action,
                }
            )
    archetype_readiness = sorted(
        [
            {
                **{key: value for key, value in stats.items() if key != "final_action_counts"},
                "final_action_counts": dict(stats["final_action_counts"].most_common()),
                "diagnosis": (
                    "confirmed_entry_available"
                    if int(stats["confirmed_entry_count"]) > 0
                    else "watch_or_near_entry_only"
                    if int(stats["watch_or_near_entry_count"]) > 0
                    else "aggregate_score_without_entry_confirmation"
                    if int(stats["candidate_count"]) > 0
                    else "no_candidates"
                ),
            }
            for stats in archetype_stats.values()
        ],
        key=lambda item: (
            int(item.get("candidate_count") or 0),
            int(item.get("confirmed_entry_count") or 0),
            int(item.get("watch_or_near_entry_count") or 0),
        ),
        reverse=True,
    )
    technical_entry_readiness = {
        "positive_or_constructive_candidate_count": int(len(positive_candidate_rows)),
        "confirmed_entry_candidate_count": int(len(technical_confirmed_rows)),
        "watch_or_near_entry_candidate_count": int(len(technical_watch_rows)),
        "ignore_or_reject_candidate_count": int(len(technical_ignore_rows)),
        "archetype_readiness": archetype_readiness[:25],
        "technical_trigger_blockers": technical_trigger_blockers,
        "buy_readiness_blockers": buy_readiness_blockers,
        "soft_failure_counts": dict(soft_failure_counts.most_common()),
        "top_soft_failures": [
            {"reason": reason, "count": int(count)}
            for reason, count in soft_failure_counts.most_common(10)
        ],
        "confirmed_entry_states": sorted(technical_entry_confirmed_states),
        "watch_only_states": sorted(technical_watch_states),
        "diagnosis": (
            "technical_entry_confirmed_candidates_available"
            if technical_confirmed_rows
            else "technical_watch_or_near_entry_only"
            if technical_watch_rows
            else "aggregate_candidate_score_without_technical_entry_confirmation"
            if positive_candidate_rows
            else "no_constructive_candidates"
        ),
        "operator_action": (
            "Do not loosen regime/context gates first; inspect why confirmed technical entry states are absent."
            if positive_candidate_rows and not technical_confirmed_rows
            else "Confirmed technical entry candidates exist; inspect downstream risk/portfolio/action consolidation."
            if technical_confirmed_rows
            else "No constructive candidate rows were available for technical-entry diagnosis."
        ),
    }
    rejection_records = rejections.to_dict(orient="records") if not rejections.empty else []
    rejection_reason_counts = Counter(str(row.get("reason_code") or "missing").strip() for row in rejection_records)
    rejection_severity_counts = Counter(str(row.get("severity") or "missing").strip().lower() for row in rejection_records)
    regime_overlay_rejections = summarize_regime_overlay_rejections(rejection_records)
    survival_counts = Counter(
        (final_action_by_symbol.get(str(row.get("symbol") or "").strip().upper()) or {}).get("action_code") or "NO_FINAL_ACTION"
        for row in positive_candidate_rows
    )
    confirmed_survival_counts = Counter(
        (final_action_by_symbol.get(str(row.get("symbol") or "").strip().upper()) or {}).get("action_code") or "NO_FINAL_ACTION"
        for row in technical_confirmed_rows
    )
    confirmed_lost_downstream_rows = [
        row
        for row in technical_confirmed_rows
        if (final_action_by_symbol.get(str(row.get("symbol") or "").strip().upper()) or {}).get("action_code") not in POSITIVE_ACTIONS
    ]
    survival_matrix_stats: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in candidate_records:
        symbol = str(row.get("symbol") or "").strip().upper()
        final = final_action_by_symbol.get(symbol) or {}
        key = (
            str(row.get("candidate_state") or "missing").strip().upper(),
            str(row.get("technical_state") or "missing").strip().upper(),
            str(row.get("technical_trigger_type") or "missing").strip().upper(),
            str(final.get("action_code") or "NO_FINAL_ACTION").strip().upper(),
            str(final.get("action_source") or "NO_FINAL_ACTION").strip(),
        )
        stats = survival_matrix_stats.setdefault(
            key,
            {
                "candidate_state": key[0],
                "technical_state": key[1],
                "technical_trigger_type": key[2],
                "final_action": key[3],
                "final_action_source": key[4],
                "row_count": 0,
                "rule_pass_count": 0,
                "watch_enabled_count": 0,
                "_setup_score_total": 0.0,
                "_setup_score_count": 0,
                "_technical_score_total": 0.0,
                "_technical_score_count": 0,
                "sample_symbols": [],
            },
        )
        stats["row_count"] += 1
        if bool(row.get("rule_pass")):
            stats["rule_pass_count"] += 1
        if bool(row.get("watch_enabled")):
            stats["watch_enabled_count"] += 1
        setup_score = _num(row.get("setup_score"))
        if setup_score is not None:
            stats["_setup_score_total"] += setup_score
            stats["_setup_score_count"] += 1
        technical_score = _num(row.get("technical_score"))
        if technical_score is not None:
            stats["_technical_score_total"] += technical_score
            stats["_technical_score_count"] += 1
        if symbol and len(stats["sample_symbols"]) < 8:
            stats["sample_symbols"].append(symbol)
    candidate_survival_matrix = []
    for stats in survival_matrix_stats.values():
        setup_count = int(stats.pop("_setup_score_count") or 0)
        setup_total = float(stats.pop("_setup_score_total") or 0.0)
        technical_count = int(stats.pop("_technical_score_count") or 0)
        technical_total = float(stats.pop("_technical_score_total") or 0.0)
        stats["avg_setup_score"] = round(setup_total / setup_count, 4) if setup_count else None
        stats["avg_technical_score"] = round(technical_total / technical_count, 4) if technical_count else None
        candidate_survival_matrix.append(stats)
    candidate_survival_matrix.sort(
        key=lambda item: (
            int(item.get("row_count") or 0),
            int(item.get("rule_pass_count") or 0),
            float(item.get("avg_setup_score") or -999.0),
        ),
        reverse=True,
    )
    technical_ignore_candidate_count = sum(
        int(item.get("row_count") or 0)
        for item in candidate_survival_matrix
        if str(item.get("technical_state") or "").upper() in technical_ignore_states
    )
    candidate_survival_diagnosis = (
        "all_candidates_technical_ignore"
        if candidate_records and technical_ignore_candidate_count == len(candidate_records)
        else "confirmed_entry_candidates_available"
        if technical_confirmed_rows
        else "watch_or_near_entry_candidates_only"
        if technical_watch_rows
        else "candidate_survival_mixed"
        if candidate_records
        else "no_candidate_rows"
    )
    return {
        "candidate_rows": int(len(candidates)),
        "rejection_rows": int(len(rejections)),
        "rule_pass_count": int(rule_pass_count),
        "watch_enabled_count": int(watch_enabled_count),
        "positive_or_constructive_candidate_count": int(len(positive_candidate_rows)),
        "pass_now_with_technical_ignore_count": int(len(pass_now_with_technical_ignore)),
        "candidate_state_counts": dict(state_counts.most_common()),
        "technical_state_counts": dict(technical_counts.most_common()),
        "technical_trigger_counts": dict(trigger_counts.most_common()),
        "rejection_reason_counts": dict(rejection_reason_counts.most_common(20)),
        "rejection_severity_counts": dict(rejection_severity_counts.most_common()),
        "regime_overlay_rejections": regime_overlay_rejections,
        "technical_entry_readiness": technical_entry_readiness,
        "positive_candidate_final_action_counts": dict(survival_counts.most_common()),
        "confirmed_entry_final_action_counts": dict(confirmed_survival_counts.most_common()),
        "confirmed_entry_lost_downstream_count": int(len(confirmed_lost_downstream_rows)),
        "candidate_survival_diagnosis": candidate_survival_diagnosis,
        "candidate_survival_matrix": candidate_survival_matrix[:40],
        "confirmed_entry_lost_downstream_samples": [
            {
                "symbol": str(row.get("symbol") or "").strip().upper(),
                "setup_id": row.get("setup_id"),
                "candidate_state": row.get("candidate_state"),
                "technical_state": row.get("technical_state"),
                "technical_trigger_type": row.get("technical_trigger_type"),
                "technical_setup_archetype": infer_calibration_setup_archetype(row),
                "setup_score": _num(row.get("setup_score")),
                "technical_score": _num(row.get("technical_score")),
                "rule_pass": bool(row.get("rule_pass")),
                "watch_enabled": bool(row.get("watch_enabled")),
                "watch_reason_detail": row.get("watch_reason_detail"),
                "final_action": (final_action_by_symbol.get(str(row.get("symbol") or "").strip().upper()) or {}).get("action_code"),
                "final_action_source": (final_action_by_symbol.get(str(row.get("symbol") or "").strip().upper()) or {}).get("action_source"),
                "final_execution_mode": (final_action_by_symbol.get(str(row.get("symbol") or "").strip().upper()) or {}).get("execution_mode"),
            }
            for row in confirmed_lost_downstream_rows[:25]
        ],
        "pass_now_with_technical_ignore_samples": [
            {
                "symbol": str(row.get("symbol") or "").strip().upper(),
                "setup_id": row.get("setup_id"),
                "candidate_state": row.get("candidate_state"),
                "technical_state": row.get("technical_state"),
                "technical_trigger_type": row.get("technical_trigger_type"),
                "technical_setup_archetype": infer_calibration_setup_archetype(row),
                "setup_score": _num(row.get("setup_score")),
                "technical_score": _num(row.get("technical_score")),
                "rule_pass": bool(row.get("rule_pass")),
                "watch_enabled": bool(row.get("watch_enabled")),
                "watch_reason_detail": row.get("watch_reason_detail"),
                "final_action": (final_action_by_symbol.get(str(row.get("symbol") or "").strip().upper()) or {}).get("action_code"),
                "final_action_source": (final_action_by_symbol.get(str(row.get("symbol") or "").strip().upper()) or {}).get("action_source"),
            }
            for row in pass_now_with_technical_ignore[:25]
        ],
        "top_positive_or_constructive_candidates": [
            {
                "symbol": str(row.get("symbol") or "").strip().upper(),
                "setup_id": row.get("setup_id"),
                "candidate_state": row.get("candidate_state"),
                "technical_state": row.get("technical_state"),
                "technical_trigger_type": row.get("technical_trigger_type"),
                "technical_setup_archetype": infer_calibration_setup_archetype(row),
                "setup_score": _num(row.get("setup_score")),
                "technical_score": _num(row.get("technical_score")),
                "rule_pass": bool(row.get("rule_pass")),
                "watch_enabled": bool(row.get("watch_enabled")),
                "watch_reason_detail": row.get("watch_reason_detail"),
                "final_action": (final_action_by_symbol.get(str(row.get("symbol") or "").strip().upper()) or {}).get("action_code"),
                "final_action_source": (final_action_by_symbol.get(str(row.get("symbol") or "").strip().upper()) or {}).get("action_source"),
                "final_execution_mode": (final_action_by_symbol.get(str(row.get("symbol") or "").strip().upper()) or {}).get("execution_mode"),
            }
            for row in positive_candidate_rows[:25]
        ],
        "diagnosis": (
            "upstream_constructive_candidates_available"
            if positive_candidate_rows
            else "upstream_candidates_exist_but_none_constructive"
            if candidate_records
            else "no_upstream_candidate_rows"
        ),
    }


def summarize_regime_overlay_rejections(rejection_records: list[dict[str, Any]]) -> dict[str, Any]:
    hard_regime_codes = {"regime_blocked", "regime_not_allowed"}
    hard_overlay_codes = {"overlay_not_allowed"}
    context_only_regime_codes = {"regime_blocked_context_only", "missing_regime_snapshot_context_only"}
    context_only_overlay_codes = {"overlay_blocked_context_only"}
    counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    for row in rejection_records:
        reason = str(row.get("reason_code") or "").strip()
        severity = str(row.get("severity") or "").strip().lower()
        bucket: str | None = None
        if reason in hard_regime_codes and severity == "hard":
            bucket = "hard_regime_label"
        elif reason in hard_overlay_codes and severity == "hard":
            bucket = "hard_overlay_label"
        elif reason in context_only_regime_codes or (reason.startswith("regime_") and severity == "soft"):
            bucket = "context_only_regime_warning"
        elif reason in context_only_overlay_codes or (reason.startswith("overlay_") and severity == "soft"):
            bucket = "context_only_overlay_warning"
        elif reason in hard_regime_codes or reason in hard_overlay_codes:
            bucket = "legacy_or_ambiguous_context_gate"
        if bucket is None:
            continue
        counts[bucket] += 1
        if len(samples) < 25:
            samples.append(
                {
                    "symbol": str(row.get("symbol") or "").strip().upper(),
                    "setup_id": row.get("setup_id"),
                    "reason_code": reason,
                    "severity": severity or None,
                    "bucket": bucket,
                    "reason_detail": row.get("reason_detail"),
                    "base_regime": row.get("base_regime"),
                    "news_overlay": row.get("news_overlay"),
                }
            )
    return {
        "counts": dict(counts.most_common()),
        "hard_context_gate_count": int(counts.get("hard_regime_label", 0) + counts.get("hard_overlay_label", 0)),
        "context_only_warning_count": int(counts.get("context_only_regime_warning", 0) + counts.get("context_only_overlay_warning", 0)),
        "samples": samples,
        "diagnosis": (
            "hard_regime_or_overlay_gates_present"
            if counts.get("hard_regime_label") or counts.get("hard_overlay_label")
            else "context_only_regime_overlay_warnings_present"
            if counts.get("context_only_regime_warning") or counts.get("context_only_overlay_warning")
            else "no_regime_overlay_rejection_pressure"
        ),
    }


def summarize_technical_artifacts(
    *,
    upstream: dict[str, Any] | None = None,
    code_data_freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    upstream = upstream if isinstance(upstream, dict) else {}
    code_data_freshness = code_data_freshness if isinstance(code_data_freshness, dict) else {}
    pass_now_ignore_count = int(upstream.get("pass_now_with_technical_ignore_count") or 0)
    samples = upstream.get("pass_now_with_technical_ignore_samples") if isinstance(upstream.get("pass_now_with_technical_ignore_samples"), list) else []
    source_staleness = (
        code_data_freshness.get("source_rows_precede_current_code_by_source")
        if isinstance(code_data_freshness.get("source_rows_precede_current_code_by_source"), dict)
        else {}
    )
    # PASS_NOW/technical_state artifacts originate from advisory_candidates and
    # candidate rejections. A fresh action-refresh row must not hide stale
    # upstream rule-engine rows.
    technical_source_rows_precede_code = bool(
        source_staleness.get("candidates")
        or source_staleness.get("candidate_rejections")
    )
    source_rows_precede_code = (
        technical_source_rows_precede_code
        if source_staleness
        else bool(code_data_freshness.get("source_rows_precede_current_code"))
    )
    technical_readiness = upstream.get("technical_entry_readiness") if isinstance(upstream.get("technical_entry_readiness"), dict) else {}
    if not pass_now_ignore_count:
        status = "no_pass_now_technical_ignore_artifacts"
        operator_action = "No stale PASS_NOW technical-ignore artifacts were detected."
        rerun_required = False
        safe_to_tune = False
    elif source_rows_precede_code:
        status = "stale_pass_now_technical_ignore_artifacts"
        operator_action = (
            "Rerun all_advisory.sh before changing technical thresholds; persisted PASS_NOW rows with technical_state=IGNORE "
            "predate the current rule-engine technical-confirmation code."
        )
        rerun_required = True
        safe_to_tune = False
    else:
        status = "current_pass_now_technical_ignore_conflict"
        operator_action = (
            "Treat PASS_NOW with technical_state=IGNORE as a current correctness issue or setup-scoring bypass; inspect rule-engine output "
            "before changing regime/context policy."
        )
        rerun_required = False
        safe_to_tune = False
    return {
        "status": status,
        "pass_now_with_technical_ignore_count": pass_now_ignore_count,
        "source_rows_precede_current_code": source_rows_precede_code,
        "technical_source_rows_precede_current_code": technical_source_rows_precede_code,
        "source_rows_precede_current_code_by_source": source_staleness,
        "rerun_required_before_policy_tuning": bool(rerun_required),
        "safe_to_tune_technical_thresholds": bool(safe_to_tune),
        "technical_entry_readiness_diagnosis": technical_readiness.get("diagnosis"),
        "samples": samples[:10],
        "operator_action": operator_action,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "separate stale technical artifacts from current technical-threshold or setup-scoring issues",
        },
    }


def current_context_gate_policy_snapshot() -> dict[str, Any]:
    rule_regime_hard = _env_bool(RULE_ENGINE_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV)
    rule_overlay_hard = _env_bool(RULE_ENGINE_OVERLAY_LABEL_HARD_BLOCK_ENABLED_ENV)
    action_regime_hard = _env_bool(ACTION_MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV)
    action_score_risk_off_hard = _env_bool(ACTION_MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED_ENV)
    action_weak_breadth_hard = _env_bool(ACTION_MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED_ENV)
    action_high_macro_risk_hard = _env_bool(ACTION_MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED_ENV)
    hypothesis_regime_hard = _env_bool(HYPOTHESIS_MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV)
    hypothesis_score_risk_off_hard = _env_bool(HYPOTHESIS_MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED_ENV)
    hypothesis_weak_breadth_hard = _env_bool(HYPOTHESIS_MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED_ENV)
    hypothesis_high_macro_risk_hard = _env_bool(HYPOTHESIS_MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED_ENV)
    exposure = summarize_single_regime_hard_gate_exposure(
        rule_regime_hard=rule_regime_hard,
        rule_overlay_hard=rule_overlay_hard,
        action_regime_hard=action_regime_hard,
        action_score_risk_off_hard=action_score_risk_off_hard,
        action_weak_breadth_hard=action_weak_breadth_hard,
        action_high_macro_risk_hard=action_high_macro_risk_hard,
        hypothesis_regime_hard=hypothesis_regime_hard,
        hypothesis_score_risk_off_hard=hypothesis_score_risk_off_hard,
        hypothesis_weak_breadth_hard=hypothesis_weak_breadth_hard,
        hypothesis_high_macro_risk_hard=hypothesis_high_macro_risk_hard,
    )
    return {
        "rule_engine_regime_label_hard_block_enabled": rule_regime_hard,
        "rule_engine_overlay_label_hard_block_enabled": rule_overlay_hard,
        "action_market_context_regime_label_hard_block_enabled": action_regime_hard,
        "action_market_context_score_risk_off_hard_block_enabled": action_score_risk_off_hard,
        "action_market_context_weak_breadth_hard_block_enabled": action_weak_breadth_hard,
        "action_market_context_high_macro_risk_hard_block_enabled": action_high_macro_risk_hard,
        "hypothesis_market_context_regime_label_hard_block_enabled": hypothesis_regime_hard,
        "hypothesis_market_context_score_risk_off_hard_block_enabled": hypothesis_score_risk_off_hard,
        "hypothesis_market_context_weak_breadth_hard_block_enabled": hypothesis_weak_breadth_hard,
        "hypothesis_market_context_high_macro_risk_hard_block_enabled": hypothesis_high_macro_risk_hard,
        "single_regime_hard_gate_exposure": exposure,
        "env": {
            RULE_ENGINE_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV: os.getenv(RULE_ENGINE_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV),
            RULE_ENGINE_OVERLAY_LABEL_HARD_BLOCK_ENABLED_ENV: os.getenv(RULE_ENGINE_OVERLAY_LABEL_HARD_BLOCK_ENABLED_ENV),
            ACTION_MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV: os.getenv(ACTION_MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV),
            ACTION_MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED_ENV: os.getenv(ACTION_MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED_ENV),
            ACTION_MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED_ENV: os.getenv(ACTION_MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED_ENV),
            ACTION_MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED_ENV: os.getenv(ACTION_MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED_ENV),
            HYPOTHESIS_MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV: os.getenv(HYPOTHESIS_MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV),
            HYPOTHESIS_MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED_ENV: os.getenv(HYPOTHESIS_MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED_ENV),
            HYPOTHESIS_MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED_ENV: os.getenv(HYPOTHESIS_MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED_ENV),
            HYPOTHESIS_MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED_ENV: os.getenv(HYPOTHESIS_MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED_ENV),
        },
        "policy_summary": {
            "rule_engine_global_regime_label": "hard_gate" if rule_regime_hard else "context_only_warning",
            "rule_engine_news_overlay_label": "hard_gate" if rule_overlay_hard else "context_only_warning",
            "action_global_regime_label": "hard_gate" if action_regime_hard else "diagnostic_or_size_overlay",
            "action_score_risk_off": "hard_gate" if action_score_risk_off_hard else "size_or_caution_overlay",
            "action_weak_breadth": "hard_gate" if action_weak_breadth_hard else "size_or_caution_overlay",
            "action_high_macro_risk": "hard_gate" if action_high_macro_risk_hard else "size_or_caution_overlay",
            "hypothesis_global_regime_label": "hard_gate" if hypothesis_regime_hard else "context_only_watch_adjustment",
            "hypothesis_score_risk_off": "hard_gate" if hypothesis_score_risk_off_hard else "context_only_watch_adjustment",
            "hypothesis_weak_breadth": "hard_gate" if hypothesis_weak_breadth_hard else "context_only_watch_adjustment",
            "hypothesis_high_macro_risk": "hard_gate" if hypothesis_high_macro_risk_hard else "context_only_watch_adjustment",
        },
    }


def summarize_single_regime_hard_gate_exposure(
    *,
    rule_regime_hard: bool,
    rule_overlay_hard: bool,
    action_regime_hard: bool,
    action_score_risk_off_hard: bool,
    action_weak_breadth_hard: bool,
    action_high_macro_risk_hard: bool = False,
    hypothesis_regime_hard: bool = False,
    hypothesis_score_risk_off_hard: bool = False,
    hypothesis_weak_breadth_hard: bool = False,
    hypothesis_high_macro_risk_hard: bool = False,
) -> dict[str, Any]:
    single_regime_flags = {
        RULE_ENGINE_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV: bool(rule_regime_hard),
        ACTION_MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV: bool(action_regime_hard),
        HYPOTHESIS_MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV: bool(hypothesis_regime_hard),
    }
    context_flags = {
        RULE_ENGINE_OVERLAY_LABEL_HARD_BLOCK_ENABLED_ENV: bool(rule_overlay_hard),
        ACTION_MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED_ENV: bool(action_score_risk_off_hard),
        ACTION_MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED_ENV: bool(action_weak_breadth_hard),
        ACTION_MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED_ENV: bool(action_high_macro_risk_hard),
        HYPOTHESIS_MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED_ENV: bool(hypothesis_score_risk_off_hard),
        HYPOTHESIS_MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED_ENV: bool(hypothesis_weak_breadth_hard),
        HYPOTHESIS_MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED_ENV: bool(hypothesis_high_macro_risk_hard),
    }
    active_single_regime_flags = [name for name, enabled in single_regime_flags.items() if enabled]
    active_context_flags = [name for name, enabled in context_flags.items() if enabled]
    if active_single_regime_flags:
        status = "active_single_regime_hard_gate"
        operator_action = (
            "A broad regime-label hard gate is enabled. Verify this is intentional before interpreting missing BUYs; "
            "the research-backed default is context-only/sizing behavior for global labels."
        )
    elif active_context_flags:
        status = "context_hard_gates_enabled_without_single_regime_gate"
        operator_action = (
            "No broad regime-label hard gate is enabled, but other hard context gates are active; inspect whether each one is backed by hard macro-risk, score-risk, or weak-breadth policy."
        )
    else:
        status = "no_single_regime_hard_gate"
        operator_action = "Broad regime labels are diagnostic/context-only by current env policy."
    return {
        "status": status,
        "active_single_regime_flags": active_single_regime_flags,
        "active_context_hard_flags": active_context_flags,
        "global_regime_label_blocks_buy": bool(active_single_regime_flags),
        "operator_action": operator_action,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "verify whether missing recommendations are caused by active global-regime hard gates versus layered context evidence",
        },
    }


def summarize_stale_context_gate_artifacts(
    *,
    upstream: dict[str, Any] | None = None,
    context_gate_policy: dict[str, Any] | None = None,
    code_data_freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    upstream = upstream if isinstance(upstream, dict) else {}
    context_gate_policy = context_gate_policy if isinstance(context_gate_policy, dict) else {}
    code_data_freshness = code_data_freshness if isinstance(code_data_freshness, dict) else {}
    regime_overlay = upstream.get("regime_overlay_rejections") if isinstance(upstream.get("regime_overlay_rejections"), dict) else {}
    aggregate_counts = regime_overlay.get("counts") if isinstance(regime_overlay.get("counts"), dict) else {}
    samples = regime_overlay.get("samples") if isinstance(regime_overlay.get("samples"), list) else []
    rule_regime_hard = bool(context_gate_policy.get("rule_engine_regime_label_hard_block_enabled"))
    rule_overlay_hard = bool(context_gate_policy.get("rule_engine_overlay_label_hard_block_enabled"))
    counts: Counter[str] = Counter()
    if not rule_regime_hard:
        counts["stale_hard_regime_label"] += int(aggregate_counts.get("hard_regime_label") or 0)
    else:
        counts["active_hard_regime_label"] += int(aggregate_counts.get("hard_regime_label") or 0)
    if not rule_overlay_hard:
        counts["stale_hard_overlay_label"] += int(aggregate_counts.get("hard_overlay_label") or 0)
    else:
        counts["active_hard_overlay_label"] += int(aggregate_counts.get("hard_overlay_label") or 0)
    stale_samples: list[dict[str, Any]] = []
    active_samples: list[dict[str, Any]] = []
    for item in samples:
        if not isinstance(item, dict):
            continue
        bucket = str(item.get("bucket") or "").strip()
        current_effect: str | None = None
        stale_artifact = False
        if bucket == "hard_regime_label":
            current_effect = "hard_gate" if rule_regime_hard else "context_only_warning"
            stale_artifact = not rule_regime_hard
        elif bucket == "hard_overlay_label":
            current_effect = "hard_gate" if rule_overlay_hard else "context_only_warning"
            stale_artifact = not rule_overlay_hard
        else:
            continue
        row = {
            **item,
            "current_policy_effect": current_effect,
            "stale_artifact": stale_artifact,
            "operator_action": (
                "Rerun all_advisory.sh; this persisted hard gate should be context-only under current env policy."
                if stale_artifact
                else "Hard gate is consistent with current env policy; inspect the underlying context before changing thresholds."
            ),
        }
        if stale_artifact:
            if len(stale_samples) < 25:
                stale_samples.append(row)
        else:
            if len(active_samples) < 10:
                active_samples.append(row)
    stale_count = int(sum(value for key, value in counts.items() if key.startswith("stale_")))
    active_count = int(sum(value for key, value in counts.items() if key.startswith("active_")))
    superseded = _stale_context_gates_superseded_by_actions(
        code_data_freshness=code_data_freshness,
        stale_hard_context_gate_count=stale_count,
    )
    return {
        "status": (
            "stale_hard_context_gate_artifacts_superseded_by_action_refresh"
            if stale_count and superseded["superseded"]
            else "stale_hard_context_gate_artifacts_present"
            if stale_count
            else "active_hard_context_gates_present"
            if active_count
            else "no_hard_context_gate_samples"
        ),
        "stale_artifact_count": stale_count,
        "active_hard_gate_count": active_count,
        "superseded_by_newer_action_rows": bool(superseded["superseded"]),
        "superseded_evidence": superseded,
        "counts": dict(counts.most_common()),
        "samples": stale_samples,
        "active_samples": active_samples,
        "operator_action": (
            "Older persisted hard regime/overlay rows were superseded by newer consolidated action recommendations; keep them as audit history, but do not treat them as the current missing-BUY blocker."
            if stale_count and superseded["superseded"]
            else "Treat persisted hard regime/overlay rows as stale artifacts until all_advisory.sh reruns under the current context-only policy."
            if stale_count
            else "No stale hard context-gate samples were detected."
            if not active_count
            else "Some persisted hard context gates are still consistent with current env policy; inspect samples before loosening policy."
        ),
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "separate stale persisted hard-gate artifacts from current context-gate policy",
        },
    }


def summarize_risk_portfolio_handoff(
    allocations: pd.DataFrame | None = None,
    portfolio: pd.DataFrame | None = None,
) -> dict[str, Any]:
    allocation_rows = allocations.to_dict(orient="records") if isinstance(allocations, pd.DataFrame) and not allocations.empty else []
    portfolio_rows = portfolio.to_dict(orient="records") if isinstance(portfolio, pd.DataFrame) and not portfolio.empty else []
    allocation_status_counts = Counter(str(row.get("allocation_status") or "missing").strip() for row in allocation_rows)
    portfolio_status_counts = Counter(str(row.get("portfolio_status") or "missing").strip() for row in portfolio_rows)
    portfolio_reason_counts = Counter(str(row.get("portfolio_reason") or "missing").strip() for row in portfolio_rows)

    base_unconfirmed_allocations = [
        row
        for row in allocation_rows
        if _boolish(row.get("is_base_candidate_fallback")) and not _boolish(row.get("technical_entry_confirmed"))
    ]
    portfolio_technical_deferred = [
        row
        for row in portfolio_rows
        if str(row.get("portfolio_reason") or "").strip() == "technical_entry_not_confirmed"
        or (_boolish(row.get("is_base_candidate_fallback")) and not _boolish(row.get("technical_entry_confirmed")))
    ]
    base_unconfirmed_allocations.sort(key=lambda row: (_num(row.get("suggested_allocation_inr")) or 0.0, str(row.get("symbol") or "")), reverse=True)
    portfolio_technical_deferred.sort(key=lambda row: (_num(row.get("approved_allocation_inr")) or 0.0, str(row.get("symbol") or "")), reverse=True)

    def allocation_sample(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "symbol": str(row.get("symbol") or "").strip().upper(),
            "setup_id": row.get("setup_id"),
            "allocation_status": row.get("allocation_status"),
            "candidate_state": row.get("candidate_state"),
            "current_state": row.get("current_state"),
            "technical_state": row.get("technical_state"),
            "technical_trigger_type": row.get("technical_trigger_type"),
            "technical_entry_confirmed": _boolish(row.get("technical_entry_confirmed")),
            "is_base_candidate_fallback": _boolish(row.get("is_base_candidate_fallback")),
            "suggested_allocation_inr": _num(row.get("suggested_allocation_inr")),
            "notes": _text(row.get("notes")),
        }

    def portfolio_sample(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "symbol": str(row.get("symbol") or "").strip().upper(),
            "setup_id": row.get("setup_id"),
            "portfolio_status": row.get("portfolio_status"),
            "portfolio_reason": row.get("portfolio_reason"),
            "recommended_action": row.get("recommended_action"),
            "candidate_state": row.get("candidate_state"),
            "current_state": row.get("current_state"),
            "technical_state": row.get("technical_state"),
            "technical_trigger_type": row.get("technical_trigger_type"),
            "technical_entry_confirmed": _boolish(row.get("technical_entry_confirmed")),
            "is_base_candidate_fallback": _boolish(row.get("is_base_candidate_fallback")),
            "approved_allocation_inr": _num(row.get("approved_allocation_inr")),
        }

    return {
        "allocation_rows": int(len(allocation_rows)),
        "portfolio_rows": int(len(portfolio_rows)),
        "allocation_status_counts": dict(allocation_status_counts.most_common()),
        "portfolio_status_counts": dict(portfolio_status_counts.most_common()),
        "portfolio_reason_counts": dict(portfolio_reason_counts.most_common(20)),
        "risk_base_unconfirmed_count": int(len(base_unconfirmed_allocations)),
        "portfolio_technical_entry_not_confirmed_count": int(len(portfolio_technical_deferred)),
        "risk_base_unconfirmed_samples": [allocation_sample(row) for row in base_unconfirmed_allocations[:25]],
        "portfolio_technical_entry_not_confirmed_samples": [portfolio_sample(row) for row in portfolio_technical_deferred[:25]],
        "diagnosis": (
            "risk_or_portfolio_blocked_unconfirmed_technical_entries"
            if base_unconfirmed_allocations or portfolio_technical_deferred
            else "no_risk_portfolio_technical_handoff_blocks_detected"
            if allocation_rows or portfolio_rows
            else "no_risk_or_portfolio_rows_loaded"
        ),
    }


def _max_frame_load_ts(frame: pd.DataFrame | None) -> pd.Timestamp | None:
    if not isinstance(frame, pd.DataFrame) or frame.empty or "load_ts" not in frame.columns:
        return None
    parsed = pd.to_datetime(frame["load_ts"], utc=True, errors="coerce").dropna()
    if parsed.empty:
        return None
    return parsed.max()


def _parse_iso_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed


def _stale_context_gates_superseded_by_actions(
    *,
    code_data_freshness: dict[str, Any] | None = None,
    stale_hard_context_gate_count: int = 0,
) -> dict[str, Any]:
    code_data_freshness = code_data_freshness if isinstance(code_data_freshness, dict) else {}
    source_load_ts = code_data_freshness.get("source_load_ts") if isinstance(code_data_freshness.get("source_load_ts"), dict) else {}
    action_ts = _parse_iso_timestamp(source_load_ts.get("action_recommendations"))
    rejection_ts = _parse_iso_timestamp(source_load_ts.get("candidate_rejections"))
    superseded = bool(stale_hard_context_gate_count > 0 and action_ts is not None and rejection_ts is not None and action_ts > rejection_ts)
    return {
        "superseded": superseded,
        "action_recommendations_load_ts": None if action_ts is None else action_ts.isoformat(),
        "candidate_rejections_load_ts": None if rejection_ts is None else rejection_ts.isoformat(),
        "reason": (
            "action_recommendations_newer_than_candidate_rejections"
            if superseded
            else "timestamp_evidence_missing_or_not_newer"
        ),
    }


def summarize_code_data_freshness(
    *,
    rows: pd.DataFrame | None = None,
    candidates: pd.DataFrame | None = None,
    rejections: pd.DataFrame | None = None,
    allocations: pd.DataFrame | None = None,
    portfolio: pd.DataFrame | None = None,
    code_files: dict[str, Path] | None = None,
) -> dict[str, Any]:
    source_load_ts = {
        "action_recommendations": _max_frame_load_ts(rows),
        "candidates": _max_frame_load_ts(candidates),
        "candidate_rejections": _max_frame_load_ts(rejections),
        "allocations": _max_frame_load_ts(allocations),
        "portfolio_orders": _max_frame_load_ts(portfolio),
    }
    available_source_ts = [ts for ts in source_load_ts.values() if ts is not None]
    latest_source_load_ts = max(available_source_ts) if available_source_ts else None
    code_mtimes: dict[str, pd.Timestamp] = {}
    for name, path in (code_files or CODE_FRESHNESS_FILES).items():
        try:
            code_mtimes[name] = pd.Timestamp.fromtimestamp(Path(path).stat().st_mtime, tz="UTC")
        except OSError as exc:
            record_local_fallback_event(
                module="advisory.recommendation_diagnostics",
                fallback_type="recommendation_diagnostics_code_file_stat_failed",
                source=str(path),
                severity="warn",
                reason="Recommendation diagnostics could not stat a tracked decision-code file while checking code/data freshness.",
                error=exc,
                metadata={"component": name},
            )
            continue
    latest_code_mtime = max(code_mtimes.values()) if code_mtimes else None
    source_precedes_code = bool(
        latest_source_load_ts is not None
        and latest_code_mtime is not None
        and latest_source_load_ts < latest_code_mtime
    )
    source_precedes_code_by_source = {
        name: bool(ts is not None and latest_code_mtime is not None and ts < latest_code_mtime)
        for name, ts in source_load_ts.items()
    }
    stale_components = [
        name
        for name, mtime in code_mtimes.items()
        if latest_source_load_ts is not None and latest_source_load_ts < mtime
    ]
    stale_components_by_source = {
        source_name: [
            component_name
            for component_name, mtime in code_mtimes.items()
            if source_ts is not None and source_ts < mtime
        ]
        for source_name, source_ts in source_load_ts.items()
    }
    return {
        "status": (
            "source_rows_precede_current_code"
            if source_precedes_code
            else "source_rows_current_with_code"
            if latest_source_load_ts is not None and latest_code_mtime is not None
            else "insufficient_timestamp_evidence"
        ),
        "latest_source_load_ts": None if latest_source_load_ts is None else latest_source_load_ts.isoformat(),
        "latest_code_mtime": None if latest_code_mtime is None else latest_code_mtime.isoformat(),
        "source_rows_precede_current_code": source_precedes_code,
        "source_rows_precede_current_code_by_source": source_precedes_code_by_source,
        "stale_code_components": stale_components,
        "stale_code_components_by_source": stale_components_by_source,
        "source_load_ts": {key: None if value is None else value.isoformat() for key, value in source_load_ts.items()},
        "code_mtimes": {key: value.isoformat() for key, value in code_mtimes.items()},
        "operator_action": (
            "Rerun all_advisory.sh before diagnosing missing BUY rows from this report; persisted rows were generated before the current decision code."
            if source_precedes_code
            else "Persisted diagnostic rows are not older than the tracked decision-code files."
            if latest_source_load_ts is not None and latest_code_mtime is not None
            else "Timestamp evidence is incomplete; use load_ts and cron logs before treating this diagnostic as current."
        ),
    }


def _latest_decision_load_ts(rows: pd.DataFrame | None) -> pd.Timestamp | None:
    if not isinstance(rows, pd.DataFrame) or rows.empty:
        return None
    for column in ["load_ts", "published_on", "asof_date"]:
        if column not in rows.columns:
            continue
        parsed = pd.to_datetime(rows[column], utc=True, errors="coerce").dropna()
        if not parsed.empty:
            return parsed.max()
    return None


def _activity_sample_select(table_name: str, columns: set[str]) -> str:
    def optional(column: str, alias: str | None = None, cast: str = "TEXT") -> str:
        output = alias or column
        if column in columns:
            return f"{column} AS {output}"
        return f"NULL::{cast} AS {output}"

    if table_name == SIGNAL_REFRESH_TABLE:
        return ",\n                ".join(
            [
                optional("refreshed_at", cast="TIMESTAMPTZ"),
                optional("asof_date", cast="TIMESTAMPTZ"),
                optional("symbol"),
                optional("signal_action", "action"),
                optional("effect_type"),
                optional("signal_source", "source"),
                optional("action_changed", cast="BOOLEAN"),
                optional("full_advisory_required", cast="BOOLEAN"),
                optional("broker_execution_allowed", cast="BOOLEAN"),
                optional("authority_scope"),
                optional("action_reason", "reason"),
                optional("load_ts", cast="TIMESTAMPTZ"),
            ]
        )
    return ",\n                ".join(
        [
            optional("matched_at", "refreshed_at", cast="TIMESTAMPTZ"),
            optional("observed_at", "asof_date", cast="TIMESTAMPTZ"),
            optional("symbol"),
            optional("expected_action", "action"),
            optional("signal_type", "effect_type"),
            optional("source_table", "source"),
            optional("match_status", cast="TEXT"),
            optional("match_score", cast="DOUBLE PRECISION"),
            "TRUE::BOOLEAN AS full_advisory_required",
            "FALSE::BOOLEAN AS broker_execution_allowed",
            "'review_input_only'::TEXT AS authority_scope",
            optional("match_reason", "reason"),
            optional("load_ts", cast="TIMESTAMPTZ"),
        ]
    )


def inspect_action_refresh_sync_state(
    latest_candidate_ts: pd.Timestamp | None,
    *,
    asof_date: pd.Timestamp | None = None,
) -> dict[str, Any]:
    latest_candidate = pd.to_datetime(latest_candidate_ts, utc=True, errors="coerce")
    expected_asof = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date is not None else pd.NaT
    expected_asof = None if pd.isna(expected_asof) else expected_asof.normalize()
    expected_asof_iso = None if expected_asof is None else expected_asof.isoformat()
    if latest_candidate_ts is None or pd.isna(latest_candidate):
        return {
            "status": "not_applicable",
            "source_name": ACTION_REFRESH_SYNC_SOURCE,
            "expected_asof_date": expected_asof_iso,
            "state_asof_date": None,
            "asof_matches": True,
            "latest_candidate_ts": None,
            "last_success_at": None,
            "last_item_ts": None,
            "current_for_latest_candidates": True,
            "action_refresh_recommended": False,
            "operator_action": "No action-refresh-eligible watcher rows were found after the latest advisory.",
        }
    try:
        row = load_sync_state(ACTION_REFRESH_SYNC_SOURCE) or {}
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="post_advisory_action_refresh_sync_state_load_failed",
            source=ACTION_REFRESH_SYNC_SOURCE,
            severity="warn",
            reason="Recommendation diagnostics could not inspect the latest bounded Action Queue refresh sync state.",
            error=exc,
            metadata={"latest_candidate_ts": latest_candidate.isoformat()},
        )
        return {
            "status": "sync_state_load_failed",
            "source_name": ACTION_REFRESH_SYNC_SOURCE,
            "expected_asof_date": expected_asof_iso,
            "state_asof_date": None,
            "asof_matches": False if expected_asof is not None else True,
            "latest_candidate_ts": latest_candidate.isoformat(),
            "last_success_at": None,
            "last_item_ts": None,
            "current_for_latest_candidates": False,
            "action_refresh_recommended": True,
            "operator_action": "Action-refresh sync state could not be inspected; run the fast action refresh command before trusting Action Queue visibility.",
            "error_type": exc.__class__.__name__,
        }
    if not row:
        return {
            "status": "missing_sync_state",
            "source_name": ACTION_REFRESH_SYNC_SOURCE,
            "expected_asof_date": expected_asof_iso,
            "state_asof_date": None,
            "asof_matches": False if expected_asof is not None else True,
            "latest_candidate_ts": latest_candidate.isoformat(),
            "last_success_at": None,
            "last_item_ts": None,
            "current_for_latest_candidates": False,
            "action_refresh_recommended": True,
            "operator_action": "No bounded Action Queue refresh sync state exists yet; run the fast action refresh command.",
        }
    last_success_at = pd.to_datetime(row.get("last_success_at"), utc=True, errors="coerce")
    last_item_ts = pd.to_datetime(row.get("last_item_ts"), utc=True, errors="coerce")
    usable_timestamps = [value for value in [last_success_at, last_item_ts] if not pd.isna(value)]
    latest_refresh_ts = max(usable_timestamps) if usable_timestamps else pd.NaT
    state_payload = row.get("state") if isinstance(row.get("state"), dict) else {}
    state_asof = pd.to_datetime(state_payload.get("asof_date"), utc=True, errors="coerce")
    state_asof = None if pd.isna(state_asof) else state_asof.normalize()
    asof_matches = expected_asof is None or (state_asof is not None and state_asof == expected_asof)
    timestamp_current = not pd.isna(latest_refresh_ts) and latest_refresh_ts >= latest_candidate
    current = bool(timestamp_current and asof_matches)
    status = "current" if current else "stale_wrong_asof" if timestamp_current and not asof_matches else "stale"
    return {
        "status": status,
        "source_name": ACTION_REFRESH_SYNC_SOURCE,
        "expected_asof_date": expected_asof_iso,
        "state_asof_date": None if state_asof is None else state_asof.isoformat(),
        "asof_matches": bool(asof_matches),
        "latest_candidate_ts": latest_candidate.isoformat(),
        "last_success_at": None if pd.isna(last_success_at) else last_success_at.isoformat(),
        "last_item_ts": None if pd.isna(last_item_ts) else last_item_ts.isoformat(),
        "timestamp_current_for_latest_candidates": bool(timestamp_current),
        "current_for_latest_candidates": bool(current),
        "action_refresh_recommended": not bool(current),
        "latest_refresh_source": state_payload.get("source"),
        "recommendation_rows": state_payload.get("recommendation_rows"),
        "symbol_count": state_payload.get("symbol_count"),
        "symbols_truncated": bool(state_payload.get("symbols_truncated")) if "symbols_truncated" in state_payload else None,
        "operator_action": (
            "Bounded Action Queue refresh is current for the latest watcher rows; use all_advisory.sh only for authoritative portfolio/risk reconciliation."
            if current
            else "Latest bounded Action Queue refresh is newer than watcher rows but belongs to a different advisory date; run the fast action refresh command for this diagnostic date."
            if status == "stale_wrong_asof"
            else "Watcher rows are newer than the latest bounded Action Queue refresh; run the fast action refresh command before inspecting Action Queue visibility."
        ),
    }


def _post_advisory_recommended_path(
    *,
    action_refresh_recommended: bool,
    action_refresh_candidate_count: int,
    action_refresh_sync: dict[str, Any] | None,
    full_advisory_required_count: int,
    total_new_rows: int,
) -> dict[str, Any]:
    action_refresh_sync = action_refresh_sync if isinstance(action_refresh_sync, dict) else {}
    action_refresh_current = bool(action_refresh_sync.get("current_for_latest_candidates"))
    has_action_refresh_candidates = int(action_refresh_candidate_count or 0) > 0
    full_advisory_required = int(full_advisory_required_count or 0) > 0
    if bool(action_refresh_recommended) and full_advisory_required:
        code = "fast_action_refresh_then_full_advisory"
        immediate_action = "run_action_refresh"
        follow_up_action = "run_full_advisory"
        operator_action = (
            "Run the bounded action refresh now for review-only Action Queue visibility, then schedule all_advisory.sh "
            "for authoritative portfolio, risk, lifecycle, and execution reconciliation."
        )
    elif bool(action_refresh_recommended):
        code = "fast_action_refresh_only"
        immediate_action = "run_action_refresh"
        follow_up_action = "none"
        operator_action = (
            "Run the bounded action refresh now for review-only Action Queue visibility; do not rerun the long advisory path "
            "unless later diagnostics show portfolio/risk reconciliation is needed."
        )
    elif has_action_refresh_candidates and action_refresh_current and full_advisory_required:
        code = "full_advisory_after_current_action_refresh"
        immediate_action = "none"
        follow_up_action = "run_full_advisory"
        operator_action = (
            "Fast Action Queue visibility is already current for the latest watcher rows; run all_advisory.sh only when "
            "authoritative portfolio, risk, lifecycle, and execution reconciliation is needed."
        )
    elif has_action_refresh_candidates and action_refresh_current:
        code = "action_refresh_current_no_followup"
        immediate_action = "none"
        follow_up_action = "none"
        operator_action = (
            "Fast Action Queue visibility is already current for the latest watcher rows; no full advisory rerun is indicated "
            "by this diagnostic."
        )
    elif full_advisory_required:
        code = "full_advisory_only"
        immediate_action = "run_full_advisory"
        follow_up_action = "none"
        operator_action = "Run all_advisory.sh after catch-up if these post-advisory watcher or wait-signal rows are still relevant."
    elif int(total_new_rows or 0) > 0:
        code = "inspect_non_escalating_activity"
        immediate_action = "inspect_samples"
        follow_up_action = "none"
        operator_action = "New post-advisory watcher evidence exists, but it is not marked for fast refresh or full advisory; inspect samples before rerunning anything."
    else:
        code = "no_operator_action"
        immediate_action = "none"
        follow_up_action = "none"
        operator_action = "No post-advisory watcher evidence requires action."
    return {
        "code": code,
        "immediate_action": immediate_action,
        "follow_up_action": follow_up_action,
        "operator_action": operator_action,
        "review_only_fast_refresh": bool(bool(action_refresh_recommended) or (has_action_refresh_candidates and action_refresh_current)),
        "full_advisory_required": bool(full_advisory_required),
        "portfolio_authority": "none",
        "broker_execution_allowed": False,
        "action_refresh_authority": "review_only_action_queue_visibility",
        "full_advisory_authority": "portfolio_risk_lifecycle_reconciliation",
    }


def load_post_advisory_activity(
    *,
    cutoff_ts: pd.Timestamp | None,
    limit: int = 25,
    asof_date: pd.Timestamp | None = None,
) -> dict[str, Any]:
    if cutoff_ts is None or pd.isna(cutoff_ts):
        return {
            "status": "no_decision_cutoff",
            "cutoff_ts": None,
            "total_new_rows": 0,
            "action_refresh_candidate_count": 0,
            "action_refresh_candidate_counts": {},
            "action_refresh_recommended": False,
            "action_refresh_command": None,
            "tables": {},
            "samples": [],
            "full_advisory_rerun_recommended": False,
            "operator_action": "No advisory decision timestamp is available, so watcher freshness cannot be compared to the latest advisory.",
        }
    cutoff_ts = pd.to_datetime(cutoff_ts, utc=True, errors="coerce")
    if pd.isna(cutoff_ts):
        return {
            "status": "invalid_decision_cutoff",
            "cutoff_ts": None,
            "total_new_rows": 0,
            "action_refresh_candidate_count": 0,
            "action_refresh_candidate_counts": {},
            "action_refresh_recommended": False,
            "action_refresh_command": None,
            "tables": {},
            "samples": [],
            "full_advisory_rerun_recommended": False,
            "operator_action": "Latest advisory decision timestamp could not be parsed; inspect recommendation load_ts before deciding reruns.",
        }
    configs = [
        {
            "table": SIGNAL_REFRESH_TABLE,
            "time_column": "refreshed_at",
            "required_columns": {"refreshed_at", "symbol", "signal_action"},
        },
        {
            "table": WAIT_SIGNAL_MATCHES_TABLE,
            "time_column": "matched_at",
            "required_columns": {"matched_at", "symbol", "expected_action"},
        },
    ]
    tables: dict[str, Any] = {}
    samples: list[dict[str, Any]] = []
    total_new_rows = 0
    full_advisory_required_count = 0
    action_refresh_candidate_count = 0
    action_refresh_candidate_counts: Counter[str] = Counter()
    latest_action_refresh_candidate_ts: pd.Timestamp | None = None
    for config in configs:
        table_name = str(config["table"])
        if not _table_exists(table_name):
            tables[table_name] = {"status": "missing_table", "new_rows": 0}
            continue
        columns = _table_columns(table_name)
        required = set(config["required_columns"])
        missing = sorted(required - columns)
        if missing:
            tables[table_name] = {"status": "missing_required_columns", "new_rows": 0, "missing_columns": missing}
            continue
        time_column = str(config["time_column"])
        if table_name == SIGNAL_REFRESH_TABLE and "full_advisory_required" in columns:
            full_advisory_required_sql = "COUNT(*) FILTER (WHERE COALESCE(full_advisory_required, FALSE))"
        elif table_name == WAIT_SIGNAL_MATCHES_TABLE:
            full_advisory_required_sql = "COUNT(*)"
        else:
            full_advisory_required_sql = "0"
        action_refresh_filter = "FALSE"
        if table_name == SIGNAL_REFRESH_TABLE and {"signal_action", "signal_source"}.issubset(columns):
            eligible_actions = ", ".join(f"'{value}'" for value in sorted(SIGNAL_REFRESH_ACTION_REFRESH_ACTIONS))
            action_refresh_filters = [
                f"UPPER(COALESCE(signal_action, '')) IN ({eligible_actions})",
                "LOWER(COALESCE(signal_source, '')) <> 'action_recommendation'",
            ]
            if "dry_run" in columns:
                action_refresh_filters.append("COALESCE(dry_run, FALSE) = FALSE")
            if "full_advisory_required" in columns:
                action_refresh_filters.append("COALESCE(full_advisory_required, TRUE) = TRUE")
            action_refresh_filter = " AND ".join(action_refresh_filters)
        try:
            summary = sql_to_df(
                f"""
                SELECT
                    COUNT(*) AS new_rows,
                    COUNT(DISTINCT symbol) AS symbol_count,
                    {full_advisory_required_sql} AS full_advisory_required_rows,
                    COUNT(*) FILTER (WHERE {action_refresh_filter}) AS action_refresh_candidate_rows,
                    MAX({time_column}) FILTER (WHERE {action_refresh_filter}) AS latest_action_refresh_candidate_ts,
                    MAX({time_column}) AS latest_activity_ts
                FROM {table_name}
                WHERE {time_column} > %s
                """,
                params=(cutoff_ts,),
            )
            new_rows = int(summary.iloc[0]["new_rows"]) if not summary.empty else 0
            symbol_count = int(summary.iloc[0]["symbol_count"]) if not summary.empty else 0
            table_full_advisory_required_count = int(summary.iloc[0]["full_advisory_required_rows"]) if not summary.empty else 0
            table_action_refresh_candidate_count = int(summary.iloc[0].get("action_refresh_candidate_rows") or 0) if not summary.empty else 0
            table_latest_action_refresh_candidate_ts = pd.to_datetime(summary.iloc[0].get("latest_action_refresh_candidate_ts"), utc=True, errors="coerce") if not summary.empty else None
            table_latest_action_refresh_candidate_ts = None if table_latest_action_refresh_candidate_ts is None or pd.isna(table_latest_action_refresh_candidate_ts) else table_latest_action_refresh_candidate_ts
            latest_activity_ts = pd.to_datetime(summary.iloc[0]["latest_activity_ts"], utc=True, errors="coerce") if not summary.empty else None
            latest_activity_ts = None if latest_activity_ts is None or pd.isna(latest_activity_ts) else latest_activity_ts
            sample_df = pd.DataFrame()
            if new_rows > 0:
                sample_df = sql_to_df(
                    f"""
                    SELECT
                        {_activity_sample_select(table_name, columns)}
                    FROM {table_name}
                    WHERE {time_column} > %s
                    ORDER BY {time_column} DESC NULLS LAST
                    LIMIT %s
                    """,
                    params=(cutoff_ts, int(limit)),
                )
            if table_name == SIGNAL_REFRESH_TABLE and table_action_refresh_candidate_count > 0:
                action_counts_df = sql_to_df(
                    f"""
                    SELECT
                        UPPER(COALESCE(signal_action, '')) AS signal_action,
                        COUNT(*) AS row_count
                    FROM {table_name}
                    WHERE {time_column} > %s
                      AND {action_refresh_filter}
                    GROUP BY UPPER(COALESCE(signal_action, ''))
                    """,
                    params=(cutoff_ts,),
                )
                if not action_counts_df.empty:
                    for _, count_row in action_counts_df.iterrows():
                        mapped_action = _map_signal_refresh_action_for_diagnostics(count_row.get("signal_action"))
                        if mapped_action:
                            action_refresh_candidate_counts[mapped_action] += int(count_row.get("row_count") or 0)
        except Exception as exc:
            record_local_fallback_event(
                module="advisory.recommendation_diagnostics",
                fallback_type="post_advisory_activity_load_failed",
                source=table_name,
                severity="warn",
                reason="Recommendation diagnostics could not load post-advisory watcher activity.",
                error=exc,
                metadata={"cutoff_ts": cutoff_ts.isoformat()},
            )
            tables[table_name] = {"status": "load_failed", "new_rows": 0, "error_type": exc.__class__.__name__}
            continue
        total_new_rows += new_rows
        full_advisory_required_count += table_full_advisory_required_count
        action_refresh_candidate_count += table_action_refresh_candidate_count
        if table_latest_action_refresh_candidate_ts is not None and (
            latest_action_refresh_candidate_ts is None or table_latest_action_refresh_candidate_ts > latest_action_refresh_candidate_ts
        ):
            latest_action_refresh_candidate_ts = table_latest_action_refresh_candidate_ts
        table_samples = sample_df.to_dict(orient="records") if isinstance(sample_df, pd.DataFrame) and not sample_df.empty else []
        for row in table_samples:
            if len(samples) < int(limit):
                mapped_action = _map_signal_refresh_action_for_diagnostics(row.get("action")) if table_name == SIGNAL_REFRESH_TABLE else None
                samples.append(
                    {
                        "table": table_name,
                        "refreshed_at": None if _timestamp(row.get("refreshed_at")) is None else _timestamp(row.get("refreshed_at")).isoformat(),
                        "asof_date": None if _timestamp(row.get("asof_date")) is None else _timestamp(row.get("asof_date")).isoformat(),
                        "symbol": str(row.get("symbol") or "").strip().upper(),
                        "action": _text(row.get("action")),
                        "effect_type": _text(row.get("effect_type")),
                        "source": _text(row.get("source")),
                        "action_changed": _boolish(row.get("action_changed")) if "action_changed" in row else None,
                        "full_advisory_required": _boolish(row.get("full_advisory_required")),
                        "broker_execution_allowed": _boolish(row.get("broker_execution_allowed")),
                        "authority_scope": _text(row.get("authority_scope")),
                        "action_refresh_eligible": bool(mapped_action),
                        "action_refresh_mapped_action": mapped_action,
                        "reason": _text(row.get("reason")),
                    }
                )
        tables[table_name] = {
            "status": "ok",
            "new_rows": new_rows,
            "symbol_count": symbol_count,
            "full_advisory_required_count": int(table_full_advisory_required_count),
            "action_refresh_candidate_count": int(table_action_refresh_candidate_count),
            "latest_activity_ts": None if latest_activity_ts is None else latest_activity_ts.isoformat(),
            "latest_action_refresh_candidate_ts": None
            if table_latest_action_refresh_candidate_ts is None
            else table_latest_action_refresh_candidate_ts.isoformat(),
        }
    action_refresh_command = None
    if action_refresh_candidate_count > 0:
        action_refresh_command = "python -m advisory.action_recommender --format text"
        cutoff_date = pd.to_datetime(cutoff_ts, utc=True, errors="coerce")
        if not pd.isna(cutoff_date):
            action_refresh_command = f"python -m advisory.action_recommender --date {cutoff_date.date().isoformat()} --format text"
    action_refresh_sync = inspect_action_refresh_sync_state(latest_action_refresh_candidate_ts, asof_date=asof_date)
    action_refresh_recommended = bool(action_refresh_candidate_count > 0 and action_refresh_sync.get("action_refresh_recommended"))
    if not action_refresh_recommended and action_refresh_sync.get("current_for_latest_candidates"):
        action_refresh_command = None
    recommended_path = _post_advisory_recommended_path(
        action_refresh_recommended=action_refresh_recommended,
        action_refresh_candidate_count=action_refresh_candidate_count,
        action_refresh_sync=action_refresh_sync,
        full_advisory_required_count=full_advisory_required_count,
        total_new_rows=total_new_rows,
    )
    return {
        "status": "new_activity_after_advisory" if total_new_rows > 0 else "no_new_activity_after_advisory",
        "cutoff_ts": cutoff_ts.isoformat(),
        "total_new_rows": int(total_new_rows),
        "full_advisory_required_count": int(full_advisory_required_count),
        "action_refresh_candidate_count": int(action_refresh_candidate_count),
        "action_refresh_candidate_counts": dict(action_refresh_candidate_counts.most_common()),
        "action_refresh_recommended": action_refresh_recommended,
        "action_refresh_command": action_refresh_command,
        "action_refresh_sync": action_refresh_sync,
        "tables": tables,
        "samples": samples,
        "full_advisory_rerun_recommended": bool(full_advisory_required_count > 0),
        "recommended_path": recommended_path,
        "operator_action": recommended_path["operator_action"],
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "action_refresh_authority": "review_only_action_queue_visibility",
            "full_advisory_still_authoritative": True,
            "decision_use": "decide whether watcher evidence warrants a full advisory rerun; do not trade from this report",
        },
    }


def summarize_diagnostic_trust(
    *,
    code_data_freshness: dict[str, Any] | None = None,
    upstream: dict[str, Any] | None = None,
    context_gate_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code_data_freshness = code_data_freshness if isinstance(code_data_freshness, dict) else {}
    upstream = upstream if isinstance(upstream, dict) else {}
    context_gate_policy = context_gate_policy if isinstance(context_gate_policy, dict) else {}
    regime_overlay = upstream.get("regime_overlay_rejections") if isinstance(upstream.get("regime_overlay_rejections"), dict) else {}
    regime_overlay_counts = regime_overlay.get("counts") if isinstance(regime_overlay.get("counts"), dict) else {}
    hard_regime_count = int(regime_overlay_counts.get("hard_regime_label") or 0)
    hard_overlay_count = int(regime_overlay_counts.get("hard_overlay_label") or 0)
    hard_context_gate_count = int(regime_overlay.get("hard_context_gate_count") or hard_regime_count + hard_overlay_count)
    unknown_hard_count = max(0, int(hard_context_gate_count - hard_regime_count - hard_overlay_count))
    rule_regime_hard_enabled = bool(context_gate_policy.get("rule_engine_regime_label_hard_block_enabled"))
    rule_overlay_hard_enabled = bool(context_gate_policy.get("rule_engine_overlay_label_hard_block_enabled"))
    rule_context_hard_enabled = bool(rule_regime_hard_enabled or rule_overlay_hard_enabled)
    stale_hard_regime_count = 0 if rule_regime_hard_enabled else hard_regime_count
    stale_hard_overlay_count = 0 if rule_overlay_hard_enabled else hard_overlay_count
    active_hard_regime_count = hard_regime_count if rule_regime_hard_enabled else 0
    active_hard_overlay_count = hard_overlay_count if rule_overlay_hard_enabled else 0
    # Legacy diagnostics may only have an aggregate count. Treat unknown hard gates
    # as stale unless both hard-gate env flags are still enabled.
    stale_hard_unknown_count = 0 if (rule_regime_hard_enabled and rule_overlay_hard_enabled) else unknown_hard_count
    active_hard_unknown_count = unknown_hard_count if (rule_regime_hard_enabled and rule_overlay_hard_enabled) else 0
    stale_hard_context_gate_count = int(stale_hard_regime_count + stale_hard_overlay_count + stale_hard_unknown_count)
    active_hard_context_gate_count = int(active_hard_regime_count + active_hard_overlay_count + active_hard_unknown_count)
    stale_context_gate_conflict = bool(stale_hard_context_gate_count > 0)
    superseded = _stale_context_gates_superseded_by_actions(
        code_data_freshness=code_data_freshness,
        stale_hard_context_gate_count=stale_hard_context_gate_count,
    )
    stale_context_gate_superseded = bool(superseded["superseded"])
    source_staleness = (
        code_data_freshness.get("source_rows_precede_current_code_by_source")
        if isinstance(code_data_freshness.get("source_rows_precede_current_code_by_source"), dict)
        else {}
    )
    upstream_candidate_rows_precede_code = bool(
        source_staleness.get("candidates")
        or source_staleness.get("candidate_rejections")
    )
    reasons: list[str] = []
    if code_data_freshness.get("source_rows_precede_current_code"):
        reasons.append("source_rows_precede_current_code")
    elif upstream_candidate_rows_precede_code and (
        int(upstream.get("candidate_rows") or 0) > 0
        or int(upstream.get("rejection_rows") or 0) > 0
        or int(upstream.get("pass_now_with_technical_ignore_count") or 0) > 0
    ):
        reasons.append("upstream_candidate_rows_precede_current_code")
    if stale_context_gate_conflict and not stale_context_gate_superseded:
        reasons.append("persisted_hard_context_gates_conflict_with_current_policy")
    if code_data_freshness.get("status") == "insufficient_timestamp_evidence":
        reasons.append("insufficient_timestamp_evidence")
    if not reasons:
        return {
            "status": "current",
            "reasons": [],
            "operator_action": (
                "Use this report for post-advisory diagnosis; older hard context-gate rows were superseded by newer action recommendations."
                if stale_context_gate_superseded
                else "Use this report for post-advisory diagnosis; verify samples before changing policy."
            ),
            "stale_context_gate_conflict": bool(stale_context_gate_conflict),
            "stale_context_gate_superseded_by_action_refresh": bool(stale_context_gate_superseded),
            "stale_context_gate_superseded_evidence": superseded,
            "upstream_candidate_rows_precede_current_code": bool(upstream_candidate_rows_precede_code),
            "source_rows_precede_current_code_by_source": source_staleness,
            "hard_context_gate_count": int(hard_context_gate_count),
            "stale_hard_context_gate_count": int(stale_hard_context_gate_count),
            "active_hard_context_gate_count": int(active_hard_context_gate_count),
            "hard_context_gate_counts_by_type": {
                "regime": hard_regime_count,
                "overlay": hard_overlay_count,
                "unknown": unknown_hard_count,
            },
            "stale_hard_context_gate_counts_by_type": {
                "regime": stale_hard_regime_count,
                "overlay": stale_hard_overlay_count,
                "unknown": stale_hard_unknown_count,
            },
            "active_hard_context_gate_counts_by_type": {
                "regime": active_hard_regime_count,
                "overlay": active_hard_overlay_count,
                "unknown": active_hard_unknown_count,
            },
            "rule_context_hard_enabled": bool(rule_context_hard_enabled),
            "rule_regime_hard_enabled": bool(rule_regime_hard_enabled),
            "rule_overlay_hard_enabled": bool(rule_overlay_hard_enabled),
        }
    if (
        "source_rows_precede_current_code" in reasons
        or "upstream_candidate_rows_precede_current_code" in reasons
        or "persisted_hard_context_gates_conflict_with_current_policy" in reasons
    ):
        status = "requires_advisory_rerun"
        operator_action = (
            "Rerun all_advisory.sh before changing thresholds, regime/context policy, or recommendation rules based on this report."
        )
        if "upstream_candidate_rows_precede_current_code" in reasons and "source_rows_precede_current_code" not in reasons:
            operator_action = (
                "Rerun all_advisory.sh before interpreting upstream candidate, technical-entry, or rejection evidence; "
                "candidate/rejection rows predate the current decision code even if action rows are newer."
            )
        if stale_context_gate_conflict and not stale_context_gate_superseded:
            operator_action = (
                "Rerun all_advisory.sh before interpreting the missing BUY state; persisted hard regime/overlay gates "
                "conflict with the current context-only policy."
            )
    else:
        status = "limited_evidence"
        operator_action = "Treat this report as partial; inspect load_ts, cron logs, and source tables before changing policy."
    return {
        "status": status,
        "reasons": reasons,
        "operator_action": operator_action,
        "stale_context_gate_conflict": stale_context_gate_conflict,
        "stale_context_gate_superseded_by_action_refresh": bool(stale_context_gate_superseded),
        "stale_context_gate_superseded_evidence": superseded,
        "upstream_candidate_rows_precede_current_code": bool(upstream_candidate_rows_precede_code),
        "source_rows_precede_current_code_by_source": source_staleness,
        "hard_context_gate_count": int(hard_context_gate_count),
        "stale_hard_context_gate_count": int(stale_hard_context_gate_count),
        "active_hard_context_gate_count": int(active_hard_context_gate_count),
        "hard_context_gate_counts_by_type": {
            "regime": hard_regime_count,
            "overlay": hard_overlay_count,
            "unknown": unknown_hard_count,
        },
        "stale_hard_context_gate_counts_by_type": {
            "regime": stale_hard_regime_count,
            "overlay": stale_hard_overlay_count,
            "unknown": stale_hard_unknown_count,
        },
        "active_hard_context_gate_counts_by_type": {
            "regime": active_hard_regime_count,
            "overlay": active_hard_overlay_count,
            "unknown": active_hard_unknown_count,
        },
        "rule_context_hard_enabled": bool(rule_context_hard_enabled),
        "rule_regime_hard_enabled": bool(rule_regime_hard_enabled),
        "rule_overlay_hard_enabled": bool(rule_overlay_hard_enabled),
    }


def _extract_feature_blockers(contract: dict[str, Any], raw_context: dict[str, Any], feature_freshness: dict[str, Any]) -> list[dict[str, Any]]:
    blockers: list[Any] = []
    evidence = contract.get("evidence") if isinstance(contract.get("evidence"), dict) else {}
    feature_section = evidence.get("feature_freshness") if isinstance(evidence.get("feature_freshness"), dict) else {}
    for candidate in [
        feature_section.get("feature_freshness_blockers"),
        raw_context.get("feature_freshness_blockers"),
        feature_freshness.get("blockers") if isinstance(feature_freshness, dict) else None,
    ]:
        if isinstance(candidate, list):
            blockers.extend(candidate)
    out: list[dict[str, Any]] = []
    for item in blockers:
        if isinstance(item, dict):
            out.append(item)
        elif item:
            out.append({"label": str(item)})
    return out


def _market_context(contract: dict[str, Any], raw_context: dict[str, Any]) -> dict[str, Any]:
    evidence = contract.get("evidence") if isinstance(contract.get("evidence"), dict) else {}
    macro = evidence.get("macro_regime") if isinstance(evidence.get("macro_regime"), dict) else {}
    adjustment = raw_context.get("market_context_adjustment_json")
    adjustment = _jsonish(adjustment, {}) if not isinstance(adjustment, dict) else adjustment
    if not isinstance(adjustment, dict):
        adjustment = {}
    diagnostics = macro.get("multi_context_diagnostics")
    if not isinstance(diagnostics, dict):
        diagnostics = adjustment.get("multi_context_diagnostics") if isinstance(adjustment.get("multi_context_diagnostics"), dict) else {}
    macro_stress = diagnostics.get("macro_stress_context") if isinstance(diagnostics.get("macro_stress_context"), dict) else {}
    breadth = diagnostics.get("breadth_context") if isinstance(diagnostics.get("breadth_context"), dict) else {}
    explicit_block_reason = macro.get("market_context_block_reason") or adjustment.get("risk_off_block_reason")
    macro_hard_risk = bool(macro.get("macro_hard_risk") if "macro_hard_risk" in macro else adjustment.get("macro_hard_risk"))
    score_risk_off_hard_block = bool(adjustment.get("score_risk_off_hard_block"))
    score_risk_off = bool(adjustment.get("score_risk_off"))
    weak_breadth_block = bool(macro.get("weak_breadth_block") if "weak_breadth_block" in macro else adjustment.get("weak_breadth_block"))
    regime_name = str(macro.get("regime_name") or adjustment.get("regime_name") or "").strip().upper()
    macro_risk_state = str(macro.get("macro_risk_state") or adjustment.get("macro_risk_state") or "").strip().upper()
    if not macro_hard_risk and macro_stress.get("label") == "hard_macro_risk":
        macro_hard_risk = True
    if not weak_breadth_block and bool(adjustment.get("weak_breadth_hard_block_enabled")) and breadth.get("label") == "weak_breadth":
        weak_breadth_block = True
    inferred_block_reason = explicit_block_reason
    if not inferred_block_reason:
        if macro_hard_risk:
            inferred_block_reason = "hard_macro_risk"
        elif score_risk_off_hard_block:
            inferred_block_reason = "score_risk_off_hard_block_enabled"
        elif weak_breadth_block:
            inferred_block_reason = "weak_breadth_hard_block_enabled"
        elif adjustment.get("regime_label_hard_block_enabled"):
            inferred_block_reason = "regime_label_hard_block_enabled"
        elif bool(adjustment.get("label_risk_off")) or regime_name in MARKET_CONTEXT_RISK_OFF_LABELS:
            inferred_block_reason = "legacy_regime_label_block"
        elif score_risk_off:
            inferred_block_reason = "legacy_score_risk_off_block"
        elif (raw_context.get("market_context_adjustment") or macro.get("market_context_adjustment") or adjustment.get("adjustment")) == "positive_action_blocked_by_market_context":
            inferred_block_reason = "legacy_market_context_block_missing_reason"
    return {
        "adjustment": raw_context.get("market_context_adjustment") or macro.get("market_context_adjustment") or adjustment.get("adjustment"),
        "reason": macro.get("market_context_adjustment_reason") or adjustment.get("reason"),
        "block_reason": inferred_block_reason,
        "block_reason_inferred": bool(inferred_block_reason and not explicit_block_reason),
        "regime_name": regime_name or None,
        "macro_risk_state": macro_risk_state or None,
        "risk_off_score": _num(macro.get("risk_off_score") or adjustment.get("risk_off_score")),
        "breadth_trend_alignment_pct": _num(macro.get("breadth_trend_alignment_pct") or adjustment.get("breadth_trend_alignment_pct")),
        "macro_hard_risk": macro_hard_risk,
        "score_risk_off_hard_block": score_risk_off_hard_block,
        "weak_breadth_block": weak_breadth_block,
        "regime_label_hard_block_enabled": bool(adjustment.get("regime_label_hard_block_enabled")),
        "weak_breadth_hard_block_enabled": bool(adjustment.get("weak_breadth_hard_block_enabled")),
        "symbol_leadership_override": bool(adjustment.get("symbol_leadership_override")),
        "context_overlay_positive_override": bool(adjustment.get("context_overlay_positive_override")),
        "diagnostics": diagnostics,
    }


def _context_overlay_alignment(contract: dict[str, Any]) -> dict[str, Any]:
    evidence = contract.get("evidence") if isinstance(contract.get("evidence"), dict) else {}
    overlays = evidence.get("context_overlays") if isinstance(evidence.get("context_overlays"), dict) else {}
    alignment = overlays.get("context_overlay_action_alignment")
    return alignment if isinstance(alignment, dict) else {}


def _context_overlay_summary(contract: dict[str, Any], raw_context: dict[str, Any]) -> dict[str, Any]:
    evidence = contract.get("evidence") if isinstance(contract.get("evidence"), dict) else {}
    overlays = evidence.get("context_overlays") if isinstance(evidence.get("context_overlays"), dict) else {}
    summary = overlays.get("context_overlay_summary")
    if isinstance(summary, dict):
        return summary
    raw_summary = raw_context.get("context_overlay_summary")
    return raw_summary if isinstance(raw_summary, dict) else {}


def _signal_quality_overlay_rule_adjustment(contract: dict[str, Any], raw_context: dict[str, Any]) -> dict[str, Any]:
    evidence = contract.get("evidence") if isinstance(contract.get("evidence"), dict) else {}
    section = evidence.get("signal_quality_overlay_rule_adjustment")
    if not isinstance(section, dict):
        section = {}
    adjustment = section.get("signal_quality_overlay_rule_adjustment") or raw_context.get("signal_quality_overlay_rule_adjustment")
    details = section.get("signal_quality_overlay_rule_adjustment_json")
    if not isinstance(details, dict):
        raw_details = raw_context.get("signal_quality_overlay_rule_adjustment_json")
        details = raw_details if isinstance(raw_details, dict) else {}
    if not adjustment and not details:
        return {}
    matched_rules = details.get("matched_rules") if isinstance(details.get("matched_rules"), list) else []
    return {
        "adjustment": _text(adjustment or details.get("adjustment")),
        "original_action_code": _text(details.get("original_action_code")),
        "adjusted_action_code": _text(details.get("adjusted_action_code")),
        "matched_rule_count": int(len([item for item in matched_rules if isinstance(item, dict)])),
        "matched_rules": [item for item in matched_rules[:3] if isinstance(item, dict)],
        "authority_scope": _text(details.get("authority_scope")) or "review_input_only",
        "portfolio_authority": _text(details.get("portfolio_authority")) or "none",
        "broker_execution_allowed": _boolish(details.get("broker_execution_allowed")),
        "reason": _text(details.get("reason")),
    }


def _transition_policy_gate(contract: dict[str, Any], raw_context: dict[str, Any]) -> dict[str, Any]:
    gate = contract.get("transition_policy_gate") if isinstance(contract.get("transition_policy_gate"), dict) else {}
    if gate:
        return gate
    if raw_context.get("action_transition_gate") == "unstable_transition_downgraded":
        return {
            "gate": "action_transition_stability",
            "original_action_code": raw_context.get("blocked_original_action_code"),
            "downgraded_action_code": None,
            "stability_status": raw_context.get("action_transition_stability_status"),
            "transition_blockers": raw_context.get("action_transition_blockers") or [],
            "policy_effect": raw_context.get("action_transition_policy_effect"),
            "broker_execution_allowed": raw_context.get("broker_execution_allowed"),
            "portfolio_authority": raw_context.get("portfolio_authority"),
            "full_advisory_required": raw_context.get("full_advisory_required"),
        }
    return {}


def _row_diagnostics(row: dict[str, Any]) -> dict[str, Any]:
    action = str(row.get("action_code") or "").strip().upper() or "UNKNOWN"
    contract = _jsonish(row.get("recommendation_reason_json"), {})
    contract = contract if isinstance(contract, dict) else {}
    raw_context = _jsonish(row.get("raw_context_json"), {})
    raw_context = raw_context if isinstance(raw_context, dict) else {}
    feature_freshness = _jsonish(row.get("feature_freshness_json"), {})
    feature_freshness = feature_freshness if isinstance(feature_freshness, dict) else {}
    evidence = contract.get("evidence") if isinstance(contract.get("evidence"), dict) else {}
    manual_review = evidence.get("manual_review") if isinstance(evidence.get("manual_review"), dict) else {}
    market = _market_context(contract, raw_context)
    overlay = _context_overlay_alignment(contract)
    overlay_summary = _context_overlay_summary(contract, raw_context)
    trusted_context_adjustment = _signal_quality_overlay_rule_adjustment(contract, raw_context)
    transition_gate = _transition_policy_gate(contract, raw_context)
    diagnostics = market.get("diagnostics") if isinstance(market.get("diagnostics"), dict) else {}
    macro_stress = diagnostics.get("macro_stress_context") if isinstance(diagnostics.get("macro_stress_context"), dict) else {}
    breadth = diagnostics.get("breadth_context") if isinstance(diagnostics.get("breadth_context"), dict) else {}
    sector_symbol = diagnostics.get("sector_symbol_context") if isinstance(diagnostics.get("sector_symbol_context"), dict) else {}
    technical = diagnostics.get("technical_confirmation_context") if isinstance(diagnostics.get("technical_confirmation_context"), dict) else {}
    feature_blockers = _extract_feature_blockers(contract, raw_context, feature_freshness)
    reason_status = str(row.get("reason_contract_status") or contract.get("status") or "").strip().lower()
    suppression_reasons: list[str] = []

    market_adjustment = str(market.get("adjustment") or "").strip()
    if market_adjustment == "positive_action_blocked_by_market_context":
        suppression_reasons.append("market_context_blocked")
    elif market_adjustment == "positive_action_size_reduced_by_market_context":
        suppression_reasons.append("market_context_size_reduced")
    if feature_blockers:
        suppression_reasons.append("feature_freshness_blocked")
    if reason_status and reason_status != "complete":
        suppression_reasons.append("reason_contract_incomplete")
    if transition_gate:
        suppression_reasons.append("transition_stability_downgraded")
    if str(overlay.get("alignment") or "") == "conflicts_with_positive_action":
        suppression_reasons.append("context_conflicts_positive")
    if macro_stress.get("label") == "hard_macro_risk":
        suppression_reasons.append("hard_macro_risk")
    if breadth.get("label") == "weak_breadth":
        suppression_reasons.append("weak_breadth")
    if sector_symbol.get("label") in {"not_in_top_context_universe", "ranked_no_leadership"}:
        suppression_reasons.append(str(sector_symbol.get("label")))
    if technical.get("label") in {"weak_or_unconfirmed", "adverse_or_exit", "unknown"}:
        suppression_reasons.append(f"technical_{technical.get('label')}")

    possible_single_regime_label_block = bool(
        market_adjustment == "positive_action_blocked_by_market_context"
        and not market.get("score_risk_off_hard_block")
        and macro_stress.get("label") != "hard_macro_risk"
        and bool(market.get("regime_label_hard_block_enabled"))
    )
    if possible_single_regime_label_block:
        suppression_reasons.append("possible_single_regime_label_block")

    transition_gate_payload = (
        {
            key: value
            for key, value in {
                "gate": transition_gate.get("gate"),
                "original_action_code": transition_gate.get("original_action_code"),
                "downgraded_action_code": transition_gate.get("downgraded_action_code") or action,
                "stability_status": transition_gate.get("stability_status"),
                "transition_blockers": transition_gate.get("transition_blockers") or [],
                "policy_effect": transition_gate.get("policy_effect"),
                "broker_execution_allowed": transition_gate.get("broker_execution_allowed"),
                "portfolio_authority": transition_gate.get("portfolio_authority"),
                "full_advisory_required": transition_gate.get("full_advisory_required"),
            }.items()
            if value not in (None, "", [], {})
        }
        if transition_gate
        else {}
    )

    return {
        "symbol": str(row.get("symbol") or "").strip().upper(),
        "action_code": action,
        "action_source": _text(row.get("action_source")),
        "setup_id": _text(row.get("setup_id")),
        "execution_mode": _text(row.get("execution_mode")),
        "transaction_type": _text(row.get("transaction_type")),
        "reason_contract_status": reason_status or None,
        "action_reason": _text(row.get("action_reason")),
        "approved_allocation_inr": _num(row.get("approved_allocation_inr")),
        "invest_score_pct": _num(row.get("invest_score_pct")),
        "suppression_reasons": list(dict.fromkeys(suppression_reasons)),
        "feature_blocker_count": len(feature_blockers),
        "manual_review": {
            key: value
            for key, value in {
                "boundary": manual_review.get("manual_review_boundary") or manual_review.get("boundary"),
                "effect": manual_review.get("manual_review_effect") or manual_review.get("effect"),
                "action_source": manual_review.get("manual_review_action_source"),
                "source_action": manual_review.get("manual_review_source_action"),
                "review_reason": manual_review.get("review_reason"),
                "operator_question": manual_review.get("operator_question"),
                "broker_execution_allowed": manual_review.get("broker_execution_allowed"),
            }.items()
            if value not in (None, "", [], {})
        },
        "transition_policy_gate": transition_gate_payload,
        "trusted_context_rule_adjustment": trusted_context_adjustment,
        "context_overlay_summary": overlay_summary,
        "market_context": {key: value for key, value in market.items() if key != "diagnostics" and value not in (None, "", [], {})},
        "multi_context_labels": {
            "breadth": breadth.get("label"),
            "macro_stress": macro_stress.get("label"),
            "sector_symbol": sector_symbol.get("label"),
            "technical": technical.get("label"),
            "context_overlay": overlay.get("alignment"),
        },
        "context_overlay_counts": {
            "positive": overlay.get("positive_count"),
            "negative": overlay.get("negative_count"),
            "watch": overlay.get("watch_count"),
            "suppressed": overlay.get("suppressed_count"),
        },
    }


def summarize_transition_policy_gates(row_diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [
        item
        for item in row_diagnostics
        if isinstance(item.get("transition_policy_gate"), dict) and item.get("transition_policy_gate")
    ]
    original_counts: Counter[str] = Counter()
    downgraded_counts: Counter[str] = Counter()
    blocker_counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    for item in rows:
        gate = item.get("transition_policy_gate") if isinstance(item.get("transition_policy_gate"), dict) else {}
        original = str(gate.get("original_action_code") or "UNKNOWN").strip().upper()
        downgraded = str(gate.get("downgraded_action_code") or item.get("action_code") or "UNKNOWN").strip().upper()
        original_counts[original] += 1
        downgraded_counts[downgraded] += 1
        blockers = gate.get("transition_blockers") if isinstance(gate.get("transition_blockers"), list) else []
        for blocker in blockers:
            _count(blocker_counts, str(blocker or "missing"))
        if len(samples) < 25:
            samples.append(
                {
                    "symbol": item.get("symbol"),
                    "setup_id": item.get("setup_id"),
                    "action_source": item.get("action_source"),
                    "original_action_code": original,
                    "downgraded_action_code": downgraded,
                    "stability_status": gate.get("stability_status"),
                    "transition_blockers": blockers,
                    "action_reason": item.get("action_reason"),
                }
            )
    return {
        "status": "transition_policy_gate_rows_present" if rows else "no_transition_policy_gate_rows",
        "transition_policy_gate_count": int(len(rows)),
        "original_action_counts": dict(original_counts.most_common()),
        "downgraded_action_counts": dict(downgraded_counts.most_common()),
        "transition_blocker_counts": dict(blocker_counts.most_common()),
        "samples": samples,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "action_policy_effect": "explain_transition_safety_downgrades_only",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
        },
    }


def transition_gates_include_entry_actions(transition_policy_gates: dict[str, Any]) -> bool:
    original_counts = transition_policy_gates.get("original_action_counts")
    if not isinstance(original_counts, dict):
        return False
    entry_actions = {"BUY", "BUY_MORE", "BUY_TRIGGERED", "ADD_ON_PULLBACK"}
    return any(int(original_counts.get(action) or 0) > 0 for action in entry_actions)


def summarize_trusted_context_rule_adjustments(row_diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [
        item
        for item in row_diagnostics
        if isinstance(item.get("trusted_context_rule_adjustment"), dict)
        and item.get("trusted_context_rule_adjustment")
    ]
    adjustment_counts: Counter[str] = Counter()
    original_action_counts: Counter[str] = Counter()
    adjusted_action_counts: Counter[str] = Counter()
    matched_rule_count = 0
    broker_authority_rows = 0
    samples: list[dict[str, Any]] = []
    for item in rows:
        adjustment = item.get("trusted_context_rule_adjustment") if isinstance(item.get("trusted_context_rule_adjustment"), dict) else {}
        adjustment_name = str(adjustment.get("adjustment") or "unknown").strip()
        original = str(adjustment.get("original_action_code") or "UNKNOWN").strip().upper()
        adjusted = str(adjustment.get("adjusted_action_code") or item.get("action_code") or "UNKNOWN").strip().upper()
        adjustment_counts[adjustment_name] += 1
        original_action_counts[original] += 1
        adjusted_action_counts[adjusted] += 1
        matched_rule_count += int(adjustment.get("matched_rule_count") or 0)
        if bool(adjustment.get("broker_execution_allowed")):
            broker_authority_rows += 1
        if len(samples) < 25:
            samples.append(
                {
                    "symbol": item.get("symbol"),
                    "action_code": item.get("action_code"),
                    "action_source": item.get("action_source"),
                    "adjustment": adjustment_name,
                    "original_action_code": original,
                    "adjusted_action_code": adjusted,
                    "matched_rule_count": int(adjustment.get("matched_rule_count") or 0),
                    "matched_rules": adjustment.get("matched_rules") or [],
                    "authority_scope": adjustment.get("authority_scope"),
                    "portfolio_authority": adjustment.get("portfolio_authority"),
                    "broker_execution_allowed": bool(adjustment.get("broker_execution_allowed")),
                    "reason": adjustment.get("reason"),
                }
            )
    if rows and broker_authority_rows:
        status = "trusted_context_rule_authority_violation"
        operator_action = "Investigate trusted context-rule adjustment rows that unexpectedly carry broker authority before trusting recommendations."
    elif rows:
        status = "trusted_context_rules_affected_actions"
        operator_action = "Trusted context rules affected Action Queue rows; inspect samples and benchmark-backed rule evidence before changing policy."
    else:
        status = "no_trusted_context_rule_adjustments"
        operator_action = "No final action rows were changed by trusted context rules in this diagnostic window."
    return {
        "status": status,
        "adjusted_row_count": int(len(rows)),
        "matched_rule_count": int(matched_rule_count),
        "broker_authority_violation_count": int(broker_authority_rows),
        "adjustment_counts": dict(adjustment_counts.most_common()),
        "original_action_counts": dict(original_action_counts.most_common()),
        "adjusted_action_counts": dict(adjusted_action_counts.most_common()),
        "samples": samples,
        "operator_action": operator_action,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "authority_scope": "review_input_only",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "show whether manually reviewed, evidence-backed context rules affected action consolidation without granting broker authority",
        },
    }


def summarize_reviewed_context_rule_eligibility(row_diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    row_count = 0
    total_rules = 0
    eligible_rules = 0
    ineligible_rules = 0
    unknown_eligibility_rules = 0
    family_counts: Counter[str] = Counter()
    ineligible_reasons: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    for item in row_diagnostics:
        summary = item.get("context_overlay_summary") if isinstance(item.get("context_overlay_summary"), dict) else {}
        rules_by_family = summary.get("reviewed_config_rules_by_family") if isinstance(summary.get("reviewed_config_rules_by_family"), dict) else {}
        aggregate_rules = summary.get("reviewed_config_aggregate_rules") if isinstance(summary.get("reviewed_config_aggregate_rules"), list) else []
        rules_seen_for_row = 0
        for family, rules in rules_by_family.items():
            if not isinstance(rules, list):
                continue
            family_name = str(family or "unknown").strip() or "unknown"
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                rules_seen_for_row += 1
                total_rules += 1
                family_counts[family_name] += 1
                eligible = rule.get("runtime_eligible_for_signal_quality_overlay_consumer")
                if eligible is True:
                    eligible_rules += 1
                elif eligible is False:
                    ineligible_rules += 1
                    reason = str(rule.get("runtime_ineligible_reason") or "runtime_ineligible_without_reason").strip()
                    ineligible_reasons[reason] += 1
                else:
                    unknown_eligibility_rules += 1
                    ineligible_reasons["runtime_eligibility_not_recorded"] += 1
                if len(samples) < 25:
                    samples.append(
                        {
                            "symbol": item.get("symbol"),
                            "action_code": item.get("action_code"),
                            "action_source": item.get("action_source"),
                            "family": family_name,
                            "overlay": rule.get("overlay"),
                            "status": rule.get("status"),
                            "action_policy_effect": rule.get("action_policy_effect"),
                            "runtime_eligible": eligible,
                            "runtime_reliability_classification": rule.get("runtime_reliability_classification"),
                            "runtime_stability_classification": rule.get("runtime_stability_classification"),
                            "runtime_ineligible_reason": rule.get("runtime_ineligible_reason"),
                            "broker_execution_allowed": bool(rule.get("broker_execution_allowed")),
                        }
                    )
        for rule in aggregate_rules:
            if not isinstance(rule, dict):
                continue
            rules_seen_for_row += 1
            total_rules += 1
            family_counts["aggregate"] += 1
            unknown_eligibility_rules += 1
            ineligible_reasons["aggregate_rule_annotation_only"] += 1
        if rules_seen_for_row:
            row_count += 1
    if eligible_rules:
        status = "eligible_reviewed_context_rules_present"
        operator_action = "Reviewed context rules are runtime-eligible on some rows; inspect whether they matched overlays and whether any action adjustments were applied."
    elif total_rules:
        status = "reviewed_context_rules_annotation_or_ineligible_only"
        operator_action = "Reviewed context rules are present but none are runtime-eligible; inspect ineligible reasons before assuming context policy is active."
    else:
        status = "no_reviewed_context_rules_in_action_rows"
        operator_action = "No reviewed context-rule metadata was present in final action rows for this diagnostic window."
    return {
        "status": status,
        "row_count": int(row_count),
        "total_rule_count": int(total_rules),
        "eligible_rule_count": int(eligible_rules),
        "ineligible_rule_count": int(ineligible_rules),
        "unknown_eligibility_rule_count": int(unknown_eligibility_rules),
        "family_counts": dict(family_counts.most_common()),
        "ineligible_reason_counts": dict(ineligible_reasons.most_common()),
        "samples": samples,
        "operator_action": operator_action,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "authority_scope": "review_input_only",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "show whether reviewed context rules are absent, annotation-only, ineligible, or runtime-eligible before interpreting missing BUYs",
        },
    }


def _classify_manual_review_item(item: dict[str, Any]) -> tuple[str, str]:
    manual = item.get("manual_review") if isinstance(item.get("manual_review"), dict) else {}
    market = item.get("market_context") if isinstance(item.get("market_context"), dict) else {}
    action_source = str(item.get("action_source") or manual.get("action_source") or "").strip().lower()
    boundary = str(manual.get("boundary") or "").strip().lower()
    source_action = str(manual.get("source_action") or "").strip().upper()
    review_reason = str(manual.get("review_reason") or item.get("action_reason") or "").strip().lower()
    action_reason = str(item.get("action_reason") or "").strip().lower()
    suppression_reasons = {str(reason or "").strip() for reason in item.get("suppression_reasons") or []}
    if item.get("action_code") != "MANUAL_REVIEW":
        return "not_manual_review", "Row is not a MANUAL_REVIEW final action."
    if (
        item.get("feature_blocker_count")
        or "feature_freshness_blocked" in suppression_reasons
        or "missing entry/current price" in action_reason
        or ("current price" in action_reason and "missing" in action_reason)
    ):
        return "operational_data_blocker", "Manual Review is caused by missing/stale price or required feature inputs."
    if market.get("block_reason") in {"legacy_regime_label_block", "legacy_score_risk_off_block", "legacy_market_context_block_missing_reason"}:
        return "stale_context_artifact", "Manual Review is caused by a legacy market-context block artifact; rerun advisory before interpretation."
    if boundary == "market_context_positive_action_review_required":
        return "market_context_review", "Manual Review is caused by current market-context caution/hard-block policy."
    if action_source in {"event_policy", "playbook", "playbook_symbol"} and source_action in {"BUY_WATCH", "REDUCE_EXPOSURE_REVIEW", "WATCH", "NO_ACTION"}:
        return "llm_resolved_review_only", "LLM/deterministic policy already mapped the event to review-only watch/de-risk/no-action evidence."
    if action_source in {"event_policy", "playbook", "playbook_symbol"} or "llm_review_manual" in review_reason:
        return "llm_unresolved_decision", "LLM/policy still has unresolved investment context that should be resolved by evaluator/playbook logic before relying on humans."
    if boundary in {"lifecycle_rebalance_horizon_review_required", "lifecycle_rebalance_stale_review_required"}:
        return "lifecycle_policy_review", "Lifecycle policy is asking for horizon/stale-position review."
    if boundary == "lifecycle_rebalance_manual_review_required":
        return "lifecycle_state_gap", "Lifecycle could not automate because required position state is missing or inconsistent."
    if boundary == "incomplete_reason_contract":
        return "contract_quality_blocker", "Action contract is incomplete and must be repaired before automation."
    return "other_review_only", "Manual Review has a review-only boundary but no more specific diagnostic bucket matched."


def summarize_manual_review_breakdown(row_diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    for item in row_diagnostics:
        if item.get("action_code") != "MANUAL_REVIEW":
            continue
        bucket, explanation = _classify_manual_review_item(item)
        counts[bucket] += 1
        source_counts[str(item.get("action_source") or "unknown")] += 1
        if len(samples) < 25:
            manual = item.get("manual_review") if isinstance(item.get("manual_review"), dict) else {}
            samples.append(
                {
                    "symbol": item.get("symbol"),
                    "action_source": item.get("action_source"),
                    "bucket": bucket,
                    "explanation": explanation,
                    "boundary": manual.get("boundary"),
                    "source_action": manual.get("source_action"),
                    "review_reason": manual.get("review_reason"),
                    "suppression_reasons": item.get("suppression_reasons") or [],
                    "action_reason": item.get("action_reason"),
                }
            )
    total = int(sum(counts.values()))
    automation_targets = {
        "refresh_data_or_prices": int(counts.get("operational_data_blocker", 0) + counts.get("lifecycle_state_gap", 0)),
        "rerun_advisory_for_stale_context": int(counts.get("stale_context_artifact", 0)),
        "improve_llm_policy_resolution": int(counts.get("llm_unresolved_decision", 0)),
        "keep_review_only_no_human_trade_decision": int(
            counts.get("llm_resolved_review_only", 0)
            + counts.get("market_context_review", 0)
            + counts.get("lifecycle_policy_review", 0)
            + counts.get("other_review_only", 0)
        ),
        "repair_contracts": int(counts.get("contract_quality_blocker", 0)),
    }
    recommended_commands: list[dict[str, str]] = []
    if automation_targets["refresh_data_or_prices"] > 0:
        recommended_commands.extend(
            [
                {
                    "command": "./complete_data.sh",
                    "purpose": "Refresh OHLCV/current-price and required feature inputs before treating data-blocked Manual Review rows as investment decisions.",
                },
                {
                    "command": "./all_advisory.sh",
                    "purpose": "Rebuild lifecycle/action rows after data refresh clears missing price or feature blockers.",
                },
            ]
        )
    if automation_targets["rerun_advisory_for_stale_context"] > 0 and not any(item["command"] == "./all_advisory.sh" for item in recommended_commands):
        recommended_commands.append(
            {
                "command": "./all_advisory.sh",
                "purpose": "Clear stale context artifacts before asking operators or LLM policy to resolve those Manual Review rows.",
            }
        )
    if automation_targets["improve_llm_policy_resolution"] > 0:
        recommended_commands.append(
            {
                "command": "python -m advisory.recommendation_diagnostics --format json",
                "purpose": "Inspect unresolved LLM/event-policy Manual Review samples and decide whether evaluator/playbook classification needs tightening.",
            }
        )
    if automation_targets["repair_contracts"] > 0:
        recommended_commands.append(
            {
                "command": "python -m advisory.recommendation_diagnostics --format json",
                "purpose": "Inspect incomplete reason-contract Manual Review samples before changing policy.",
            }
        )
    return {
        "status": "manual_review_rows_present" if total else "no_manual_review_rows",
        "total_manual_review_rows": total,
        "bucket_counts": dict(counts.most_common()),
        "source_counts": dict(source_counts.most_common()),
        "automation_targets": automation_targets,
        "recommended_commands": recommended_commands,
        "samples": samples,
        "operator_action": (
            "Prioritize data refresh, stale-context rerun, or LLM policy-resolution work based on bucket_counts; do not treat every MANUAL_REVIEW row as a human investment decision."
            if total
            else "No MANUAL_REVIEW rows are present."
        ),
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "separate operational/stale/LLM-resolution work from true manual investment review",
        },
    }


def summarize_context_overlay_pressure(row_diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    for item in row_diagnostics:
        overlay_counts = item.get("context_overlay_counts") if isinstance(item.get("context_overlay_counts"), dict) else {}
        overlay_summary = item.get("context_overlay_summary") if isinstance(item.get("context_overlay_summary"), dict) else {}
        positive = int(_num(overlay_counts.get("positive")) or 0)
        watch = int(_num(overlay_counts.get("watch")) or 0)
        negative = int(_num(overlay_counts.get("negative")) or 0)
        suppressed = int(_num(overlay_counts.get("suppressed")) or 0)
        if positive:
            counts["positive"] += positive
        if watch:
            counts["watch"] += watch
        if negative:
            counts["negative"] += negative
        if suppressed:
            counts["suppressed"] += suppressed
        has_pressure = bool(positive or watch or negative or suppressed)
        if has_pressure:
            action = str(item.get("action_code") or "UNKNOWN").strip().upper()
            action_counts[action] += 1
            family_counts = _context_source_family_counts_from_summary(overlay_summary)
            for family, count in family_counts.items():
                source_counts[family] += int(count)
            if len(samples) < 25:
                samples.append(
                    {
                        "symbol": item.get("symbol"),
                        "action_code": action,
                        "action_source": item.get("action_source"),
                        "execution_mode": item.get("execution_mode"),
                        "context_overlay_counts": {
                            "positive": positive,
                            "watch": watch,
                            "negative": negative,
                            "suppressed": suppressed,
                        },
                        "context_overlay_alignment": (item.get("multi_context_labels") or {}).get("context_overlay"),
                        "context_source_family_counts": family_counts,
                        "suppression_reasons": item.get("suppression_reasons") or [],
                    }
                )
    actionable_watch_pressure = int(counts.get("positive", 0) + counts.get("watch", 0))
    de_risk_pressure = int(counts.get("negative", 0))
    return {
        "status": (
            "positive_or_watch_context_pressure_present"
            if actionable_watch_pressure
            else "negative_context_pressure_present"
            if de_risk_pressure
            else "suppressed_context_only"
            if counts.get("suppressed")
            else "no_context_overlay_pressure"
        ),
        "counts": dict(counts.most_common()),
        "row_action_counts": dict(action_counts.most_common()),
        "context_source_family_counts": dict(source_counts.most_common()),
        "positive_or_watch_pressure_count": actionable_watch_pressure,
        "negative_pressure_count": de_risk_pressure,
        "suppressed_context_count": int(counts.get("suppressed", 0)),
        "samples": samples,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "action_policy_effect": "context_overlays_create_watch_or_derisk_pressure_only",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "explain whether news/macro/announcement/bhavcopy context found watch pressure but still needs technical/risk/lifecycle confirmation",
        },
    }


def _normalize_context_source_family(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    aliases = {
        "announcement": "announcement_context",
        "announcements": "announcement_context",
        "announcement_context": "announcement_context",
        "bhavcopy": "bhavcopy_context",
        "bhavcopy_context": "bhavcopy_context",
        "exchange": "exchange_context",
        "exchange_context": "exchange_context",
        "macro": "macro_context",
        "macro_context": "macro_context",
        "theme": "theme_context",
        "theme_context": "theme_context",
        "news": "theme_context",
        "news_theme": "theme_context",
    }
    return aliases.get(text, text)


def _safe_int(value: Any) -> int:
    numeric = pd.to_numeric(value, errors="coerce")
    return 0 if pd.isna(numeric) else int(numeric)


def _context_source_family_counts_from_summary(summary: dict[str, Any] | None) -> dict[str, int]:
    """Extract source-family counts from the loose context-overlay summary shapes used by action rows."""

    if not isinstance(summary, dict):
        return {}
    counts: Counter[str] = Counter()
    for key in [
        "context_source_family_counts",
        "source_family_counts",
        "context_source_counts",
        "source_counts",
        "family_counts",
    ]:
        value = summary.get(key)
        if isinstance(value, dict):
            for family, count in value.items():
                counts[_normalize_context_source_family(family)] += _safe_int(count)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    family = item.get("context_source") or item.get("source_family") or item.get("family") or item.get("source")
                    counts[_normalize_context_source_family(family)] += _safe_int(item.get("count") or item.get("row_count") or 1)
                else:
                    counts[_normalize_context_source_family(item)] += 1
    for key in [
        "families",
        "source_families",
        "context_sources",
        "sources",
        "positive_sources",
        "watch_sources",
        "negative_sources",
        "suppressed_sources",
    ]:
        value = summary.get(key)
        if isinstance(value, dict):
            for family, details in value.items():
                if isinstance(details, dict):
                    counts[_normalize_context_source_family(family)] += _safe_int(
                        details.get("count") or details.get("row_count") or details.get("matched_count") or 1
                    )
                else:
                    counts[_normalize_context_source_family(family)] += _safe_int(details)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    family = item.get("context_source") or item.get("source_family") or item.get("family") or item.get("source")
                    counts[_normalize_context_source_family(family)] += _safe_int(item.get("count") or item.get("row_count") or 1)
                else:
                    counts[_normalize_context_source_family(item)] += 1
    return {family: int(count) for family, count in counts.items() if int(count) > 0}


def summarize_context_to_entry_source_family_attribution(
    *,
    context_overlay_source_diagnostics: dict[str, Any] | None = None,
    context_watchlist_intake: dict[str, Any] | None = None,
    context_overlay_pressure: dict[str, Any] | None = None,
    context_overlay_refresh_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attribute context-to-entry bottlenecks by source family without granting trading authority."""

    source_diag = context_overlay_source_diagnostics if isinstance(context_overlay_source_diagnostics, dict) else {}
    watchlist = context_watchlist_intake if isinstance(context_watchlist_intake, dict) else {}
    pressure = context_overlay_pressure if isinstance(context_overlay_pressure, dict) else {}
    preview = context_overlay_refresh_preview if isinstance(context_overlay_refresh_preview, dict) else {}
    conversion = preview.get("conversion_diagnostics") if isinstance(preview.get("conversion_diagnostics"), dict) else {}

    source_rows: Counter[str] = Counter()
    active_rows: Counter[str] = Counter()
    positive_source_rows: Counter[str] = Counter()
    negative_source_rows: Counter[str] = Counter()
    raw_target_rows: Counter[str] = Counter()
    kept_target_rows: Counter[str] = Counter()
    reliability_suppressed_rows: Counter[str] = Counter()
    policy_suppressed_rows: Counter[str] = Counter()
    identity_policy_suppressed_rows: Counter[str] = Counter()
    watchlist_rows: Counter[str] = Counter()
    pressure_rows: Counter[str] = Counter()
    policy_suppression_reasons_by_family: dict[str, Counter[str]] = {}
    samples_by_family: dict[str, dict[str, Any]] = {}
    source_diag_asof = pd.to_datetime(source_diag.get("asof_date"), utc=True, errors="coerce")
    preview_diag = preview.get("diagnostics") if isinstance(preview.get("diagnostics"), dict) else {}
    preview_asof = pd.to_datetime(preview_diag.get("asof_date") or preview.get("asof_date"), utc=True, errors="coerce")
    command_asof = preview_asof if not pd.isna(preview_asof) else source_diag_asof
    command_date = None if pd.isna(command_asof) else command_asof.date().isoformat()

    for row in source_diag.get("sources") or []:
        if not isinstance(row, dict):
            continue
        family = _normalize_context_source_family(row.get("source") or row.get("context_source") or row.get("source_family"))
        source_rows[family] += _safe_int(row.get("row_count"))
        active_rows[family] += _safe_int(row.get("active_authorized_rows") or row.get("active_authorized_row_count"))
        positive_source_rows[family] += _safe_int(row.get("positive_watch_rows") or row.get("positive_rows") or row.get("watch_rows"))
        negative_source_rows[family] += _safe_int(row.get("negative_rows"))
        samples_by_family.setdefault(family, {})["source_sample"] = row

    for family, count in (watchlist.get("context_source_counts") or {}).items():
        watchlist_rows[_normalize_context_source_family(family)] += _safe_int(count)
    for row in watchlist.get("samples") or []:
        if isinstance(row, dict):
            samples_by_family.setdefault(_normalize_context_source_family(row.get("context_source")), {})["watchlist_sample"] = row

    for family, count in (pressure.get("context_source_family_counts") or {}).items():
        pressure_rows[_normalize_context_source_family(family)] += _safe_int(count)
    for row in pressure.get("samples") or []:
        if isinstance(row, dict):
            for family in _context_source_family_counts_from_summary(
                {"context_source_family_counts": row.get("context_source_family_counts")}
            ):
                samples_by_family.setdefault(family, {})["action_pressure_sample"] = row

    for row in conversion.get("sources") or []:
        if not isinstance(row, dict):
            continue
        family = _normalize_context_source_family(row.get("source") or row.get("context_source") or row.get("source_family"))
        raw_target_rows[family] += _safe_int(row.get("raw_target_rows") or row.get("raw_rows") or row.get("target_rows"))
        kept_target_rows[family] += _safe_int(row.get("kept_target_rows") or row.get("kept_rows") or row.get("signal_rows"))
        reliability_suppressed_rows[family] += _safe_int(row.get("reliability_suppressed_target_rows"))
        policy_suppressed_rows[family] += _safe_int(row.get("policy_suppressed_target_rows"))
        identity_policy_suppressed_rows[family] += _safe_int(row.get("identity_policy_suppressed_target_rows"))
        reason_counts = row.get("policy_suppression_reason_counts") if isinstance(row.get("policy_suppression_reason_counts"), dict) else {}
        for reason, count in reason_counts.items():
            reason_text = str(reason or "").strip()
            if reason_text:
                policy_suppression_reasons_by_family.setdefault(family, Counter())[reason_text] += _safe_int(count)
        samples_by_family.setdefault(family, {})["conversion_sample"] = row

    rows: list[dict[str, Any]] = []
    blocker_counts: Counter[str] = Counter()
    families = sorted(
        set(source_rows)
        | set(active_rows)
        | set(positive_source_rows)
        | set(negative_source_rows)
        | set(raw_target_rows)
        | set(kept_target_rows)
        | set(reliability_suppressed_rows)
        | set(policy_suppressed_rows)
        | set(identity_policy_suppressed_rows)
        | set(watchlist_rows)
        | set(pressure_rows)
    )
    for family in families:
        source_count = int(source_rows.get(family, 0))
        active_count = int(active_rows.get(family, 0))
        positive_count = int(positive_source_rows.get(family, 0))
        raw_count = int(raw_target_rows.get(family, 0))
        kept_count = int(kept_target_rows.get(family, 0))
        watch_count = int(watchlist_rows.get(family, 0))
        pressure_count = int(pressure_rows.get(family, 0))
        reliability_suppressed_count = int(reliability_suppressed_rows.get(family, 0))
        policy_suppressed_count = int(policy_suppressed_rows.get(family, 0))
        identity_policy_suppressed_count = int(identity_policy_suppressed_rows.get(family, 0))
        policy_reason_counts = dict((policy_suppression_reasons_by_family.get(family) or Counter()).most_common())
        source_refresh_command = (
            f"python -m advisory.context_overlay_refresh --from-date {command_date} --to-date {command_date} --format text"
            if command_date
            else "python -m advisory.context_overlay_refresh --format text"
        )
        preview_signal_refresh_command = str(preview.get("recommended_command") or "").strip()
        signal_refresh_command = (
            preview_signal_refresh_command
            if preview_signal_refresh_command.startswith("python -m advisory.signal_refresh")
            else (
                f"python -m advisory.signal_refresh --from-context-overlays --asof-date {command_date} --limit 50 --format text"
                if command_date
                else "python -m advisory.signal_refresh --from-context-overlays --limit 50 --format text"
            )
        )
        action_refresh_command = (
            f"python -m advisory.action_recommender --date {command_date} --format text"
            if command_date
            else "python -m advisory.action_recommender --format text"
        )
        if source_count <= 0 and active_count <= 0 and raw_count <= 0 and watch_count <= 0 and pressure_count <= 0:
            blocker = "source_family_source_rows_missing"
        elif active_count <= 0 and source_count > 0:
            blocker = "source_rows_not_active_authorized"
        elif reliability_suppressed_count > 0 and kept_count <= 0 and watch_count <= 0 and pressure_count <= 0:
            blocker = "source_family_reliability_suppressed"
        elif identity_policy_suppressed_count > 0 and kept_count <= 0 and watch_count <= 0 and pressure_count <= 0:
            blocker = "source_family_identity_unresolved"
        elif policy_suppressed_count > 0 and kept_count <= 0 and watch_count <= 0 and pressure_count <= 0:
            blocker = "source_family_policy_suppressed"
        elif positive_count > 0 and raw_count <= 0 and watch_count <= 0 and pressure_count <= 0:
            blocker = "source_to_target_conversion_gap"
        elif raw_count > 0 and kept_count <= 0 and watch_count <= 0 and pressure_count <= 0:
            blocker = "target_rows_not_kept"
        elif kept_count > 0 and watch_count <= 0 and pressure_count <= 0:
            blocker = "target_to_watchlist_or_action_gap"
        elif watch_count > 0 and pressure_count <= 0:
            blocker = "watchlist_to_action_pressure_gap"
        elif pressure_count > 0:
            blocker = "family_reached_action_pressure"
        else:
            blocker = "no_positive_watch_path_observed"
        if blocker in {"source_family_source_rows_missing", "source_to_target_conversion_gap"}:
            recommended_command = source_refresh_command
            recommended_purpose = (
                "Refresh context-overlay source/target rows for this source family before diagnosing downstream watchlist or action conversion."
            )
        elif blocker == "target_to_watchlist_or_action_gap":
            recommended_command = signal_refresh_command
            recommended_purpose = (
                "Run bounded context-overlay signal refresh so kept context targets can be reconciled into watch/action pressure."
            )
        elif blocker == "watchlist_to_action_pressure_gap":
            recommended_command = action_refresh_command
            recommended_purpose = (
                "Rebuild consolidated action recommendations from current watchlist pressure before changing source policy."
            )
        else:
            recommended_command = None
            recommended_purpose = None
        blocker_counts[blocker] += 1
        rows.append(
            {
                "context_source_family": family,
                "source_rows": source_count,
                "active_authorized_rows": active_count,
                "positive_watch_source_rows": positive_count,
                "negative_source_rows": int(negative_source_rows.get(family, 0)),
                "raw_target_rows": raw_count,
                "kept_target_rows": kept_count,
                "reliability_suppressed_target_rows": reliability_suppressed_count,
                "policy_suppressed_target_rows": policy_suppressed_count,
                "identity_policy_suppressed_target_rows": identity_policy_suppressed_count,
                "policy_suppression_reason_counts": policy_reason_counts,
                "watchlist_rows": watch_count,
                "action_pressure_rows": pressure_count,
                "primary_blocker": blocker,
                "recommended_command": recommended_command,
                "recommended_command_purpose": recommended_purpose,
                "samples": samples_by_family.get(family, {}),
            }
        )
    rows.sort(
        key=lambda row: (
            row["primary_blocker"] == "family_reached_action_pressure",
            row["action_pressure_rows"],
            row["watchlist_rows"],
            row["kept_target_rows"],
            row["active_authorized_rows"],
        ),
        reverse=True,
    )
    recommended_commands: list[dict[str, str]] = []
    for row in rows:
        command = str(row.get("recommended_command") or "").strip()
        if not command:
            continue
        if any(item.get("command") == command for item in recommended_commands):
            continue
        recommended_commands.append(
            {
                "command": command,
                "purpose": f"{row.get('context_source_family')}: {row.get('recommended_command_purpose') or row.get('primary_blocker')}",
            }
        )
    return {
        "status": "source_family_attribution_available" if rows else "no_context_source_family_evidence",
        "family_count": int(len(rows)),
        "blocker_counts": dict(blocker_counts.most_common()),
        "identity_policy_suppressed_target_rows": int(sum(identity_policy_suppressed_rows.values())),
        "recommended_commands": recommended_commands,
        "families": rows[:25],
        "operator_action": (
            "Refresh missing context-overlay source families before diagnosing conversion. Then inspect source-family blockers before changing global regime/context policy; repair conversion, reliability, watchlist, or action-refresh gaps for the affected family."
            if recommended_commands
            else "Inspect source-family blockers before changing global regime/context policy; repair conversion, reliability, watchlist, or action-refresh gaps for the affected family."
            if rows
            else "No context source-family evidence was available for attribution."
        ),
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "show which context source family is blocked between source rows and entry pressure without granting context-only BUY authority",
        },
    }


def _top_context_source_family_blockers(
    source_family_attribution: dict[str, Any] | None,
    *,
    limit: int = 5,
    include_reached: bool = False,
) -> list[dict[str, Any]]:
    payload = source_family_attribution if isinstance(source_family_attribution, dict) else {}
    families = payload.get("families") if isinstance(payload.get("families"), list) else []
    rows: list[dict[str, Any]] = []
    for item in families:
        if not isinstance(item, dict):
            continue
        blocker = str(item.get("primary_blocker") or "").strip()
        if not include_reached and blocker == "family_reached_action_pressure":
            continue
        rows.append(
            {
                "context_source_family": _normalize_context_source_family(item.get("context_source_family")),
                "primary_blocker": blocker or "unknown",
                "source_rows": _safe_int(item.get("source_rows")),
                "active_authorized_rows": _safe_int(item.get("active_authorized_rows")),
                "positive_watch_source_rows": _safe_int(item.get("positive_watch_source_rows")),
                "raw_target_rows": _safe_int(item.get("raw_target_rows")),
                "kept_target_rows": _safe_int(item.get("kept_target_rows")),
                "watchlist_rows": _safe_int(item.get("watchlist_rows")),
                "action_pressure_rows": _safe_int(item.get("action_pressure_rows")),
                "reliability_suppressed_target_rows": _safe_int(item.get("reliability_suppressed_target_rows")),
                "policy_suppressed_target_rows": _safe_int(item.get("policy_suppressed_target_rows")),
            }
        )
    rows.sort(
        key=lambda row: (
            row["action_pressure_rows"],
            row["watchlist_rows"],
            row["kept_target_rows"],
            row["raw_target_rows"],
            row["active_authorized_rows"],
            row["source_rows"],
        ),
        reverse=True,
    )
    return rows[: max(0, int(limit))]


def _format_context_source_family_blockers(source_family_attribution: dict[str, Any] | None, *, limit: int = 5) -> str:
    rows = _top_context_source_family_blockers(source_family_attribution, limit=limit)
    if not rows:
        return "[]"
    compact = []
    for row in rows:
        compact.append(
            (
                f"{row['context_source_family']}:{row['primary_blocker']}"
                f"(source={row['source_rows']},active={row['active_authorized_rows']},"
                f"raw={row['raw_target_rows']},kept={row['kept_target_rows']},"
                f"watch={row['watchlist_rows']},pressure={row['action_pressure_rows']})"
            )
        )
    return "[" + "; ".join(compact) + "]"


def _format_context_source_family_commands(source_family_attribution: dict[str, Any] | None, *, limit: int = 3) -> str:
    payload = source_family_attribution if isinstance(source_family_attribution, dict) else {}
    commands = payload.get("recommended_commands") if isinstance(payload.get("recommended_commands"), list) else []
    compact: list[str] = []
    for item in commands:
        if not isinstance(item, dict):
            continue
        command = str(item.get("command") or "").strip()
        if not command:
            continue
        compact.append(command)
        if len(compact) >= limit:
            break
    return "[" + "; ".join(compact) + "]" if compact else "[]"


def summarize_positive_context_entry_blocker(
    *,
    positive_watch_pressure: int = 0,
    active_context_watch_rows: int = 0,
    context_watchlist_current: bool = False,
    technical_precheck_coverage: int = 0,
    technical_precheck_missing: int = 0,
    technical_precheck_constructive: int = 0,
    technical_precheck_breakout_like: int = 0,
    technical_precheck_stale: bool = False,
    technical_refresh_issue_symbols: int = 0,
    hard_input_blockers: int = 0,
    stale_history_blockers: int = 0,
    constructive_candidates: int = 0,
    confirmed_entries: int = 0,
    final_positive_actions: int = 0,
    confirmed_lost_downstream: int = 0,
    benchmark_label: str = "unknown",
) -> dict[str, Any]:
    """Explain why positive context pressure has not become a BUY.

    This is attribution only. Context pressure is allowed to explain watch
    priority, but it should not create broker-capable entry authority unless
    the technical and downstream gates already support that transition.
    """

    positive_watch_pressure = int(positive_watch_pressure or 0)
    active_context_watch_rows = int(active_context_watch_rows or 0)
    technical_precheck_coverage = int(technical_precheck_coverage or 0)
    technical_precheck_missing = int(technical_precheck_missing or 0)
    technical_precheck_constructive = int(technical_precheck_constructive or 0)
    technical_precheck_breakout_like = int(technical_precheck_breakout_like or 0)
    technical_refresh_issue_symbols = int(technical_refresh_issue_symbols or 0)
    hard_input_blockers = int(hard_input_blockers or 0)
    stale_history_blockers = int(stale_history_blockers or 0)
    constructive_candidates = int(constructive_candidates or 0)
    confirmed_entries = int(confirmed_entries or 0)
    final_positive_actions = int(final_positive_actions or 0)
    confirmed_lost_downstream = int(confirmed_lost_downstream or 0)

    if positive_watch_pressure <= 0 and active_context_watch_rows <= 0:
        blocker = "no_positive_context_pressure"
        status = "not_applicable"
        operator_action = "No positive context/watch pressure is visible, so this diagnostic cannot explain absent BUY rows through the context-to-entry path."
    elif active_context_watch_rows > 0 and not context_watchlist_current:
        blocker = "context_watchlist_reconcile_required"
        status = "blocked_before_technical_confirmation"
        operator_action = "Positive context reached watchlist rows, but the context-watch snapshot is not current; reconcile context-watch rows before tuning technical or regime policy."
    elif hard_input_blockers > 0:
        blocker = "hard_ohlcv_input_blocker"
        status = "blocked_before_technical_confirmation"
        operator_action = "Positive context reached watch rows, but at least one watched symbol has hard OHLCV input blockers; repair/suppress those symbols before expecting BUYs."
    elif technical_precheck_coverage <= 0 and active_context_watch_rows > 0:
        blocker = "technical_feature_coverage_missing"
        status = "blocked_before_technical_confirmation"
        operator_action = "Positive context reached watch rows, but point-in-time technical feature coverage is missing; refresh OHLCV/technical features before changing policy."
    elif technical_refresh_issue_symbols > 0:
        blocker = "technical_refresh_issue"
        status = "blocked_before_technical_confirmation"
        operator_action = "Positive context reached watch rows, but recent technical refresh issues exist; inspect per-symbol refresh status before changing entry thresholds."
    elif technical_precheck_stale or stale_history_blockers > 0:
        blocker = "technical_feature_stale"
        status = "blocked_before_technical_confirmation"
        operator_action = "Positive context reached watch rows, but technical inputs are stale; refresh technical features and rerun diagnostics."
    elif constructive_candidates <= 0 and technical_precheck_constructive > 0:
        blocker = "candidate_generation_gap_after_constructive_precheck"
        status = "blocked_at_candidate_generation"
        operator_action = "Context-watch symbols have constructive technical precheck evidence, but no constructive rule-engine candidates; rerun bounded advisory/candidate generation."
    elif constructive_candidates <= 0:
        blocker = "no_constructive_technical_candidate"
        status = "waiting_for_setup_quality"
        operator_action = "Positive context exists, but no constructive technical candidate is visible; keep it watch-only until structure/volume/relative-strength evidence improves."
    elif confirmed_entries <= 0:
        blocker = "technical_entry_confirmation_pending"
        status = "waiting_for_entry_trigger"
        operator_action = "Constructive candidates exist, but no confirmed technical entry trigger fired; do not create context-only BUY authority."
    elif confirmed_entries > 0 and final_positive_actions <= 0:
        blocker = "downstream_consolidation_or_transition_gate"
        status = "blocked_after_entry_confirmation"
        operator_action = "Confirmed technical entries exist, but final BUY/BUY_MORE did not survive risk, lifecycle, transition, conflict, or portfolio consolidation."
    else:
        blocker = "positive_actions_present"
        status = "not_blocked"
        operator_action = "Positive actions exist; inspect risk, sizing, lifecycle, and execution readiness rather than missing-BUY causes."

    return {
        "status": status,
        "primary_blocker": blocker,
        "benchmark_participation_label": str(benchmark_label or "unknown"),
        "positive_or_watch_context_pressure_count": positive_watch_pressure,
        "active_context_watchlist_rows": active_context_watch_rows,
        "context_watchlist_current": bool(context_watchlist_current),
        "technical_precheck_coverage_count": technical_precheck_coverage,
        "technical_precheck_missing_count": technical_precheck_missing,
        "technical_precheck_constructive_count": technical_precheck_constructive,
        "technical_precheck_breakout_like_count": technical_precheck_breakout_like,
        "technical_precheck_stale": bool(technical_precheck_stale),
        "technical_refresh_issue_symbol_count": technical_refresh_issue_symbols,
        "hard_input_blocker_symbol_count": hard_input_blockers,
        "stale_history_blocker_symbol_count": stale_history_blockers,
        "constructive_candidate_count": constructive_candidates,
        "confirmed_entry_candidate_count": confirmed_entries,
        "confirmed_entry_lost_downstream_count": confirmed_lost_downstream,
        "final_positive_action_count": final_positive_actions,
        "operator_action": operator_action,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "rank why positive context/watch pressure did not become a technical BUY without granting context-only entry authority",
        },
    }


def summarize_context_to_entry_pipeline(
    *,
    context_overlay_pressure: dict[str, Any] | None = None,
    upstream: dict[str, Any] | None = None,
    action_counts: Counter[str] | dict[str, int] | None = None,
    context_overlay_source_diagnostics: dict[str, Any] | None = None,
    context_watchlist_intake: dict[str, Any] | None = None,
    context_watch_technical_precheck: dict[str, Any] | None = None,
    context_overlay_refresh_preview: dict[str, Any] | None = None,
    market_participation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read-only funnel from context evidence to final entry actions.

    This is intentionally diagnostic-only. Context/news/macro/bhavcopy evidence
    can explain watch pressure or de-risk pressure, but it must not become
    broker authority without technical, liquidity, risk, and lifecycle gates.
    """

    context_overlay_pressure = context_overlay_pressure if isinstance(context_overlay_pressure, dict) else {}
    upstream = upstream if isinstance(upstream, dict) else {}
    action_counts = action_counts if isinstance(action_counts, (Counter, dict)) else {}
    context_overlay_source_diagnostics = context_overlay_source_diagnostics if isinstance(context_overlay_source_diagnostics, dict) else {}
    context_watchlist_intake = context_watchlist_intake if isinstance(context_watchlist_intake, dict) else {}
    context_watch_technical_precheck = context_watch_technical_precheck if isinstance(context_watch_technical_precheck, dict) else {}
    context_overlay_refresh_preview = context_overlay_refresh_preview if isinstance(context_overlay_refresh_preview, dict) else {}
    market_participation_context = summarize_market_participation_context(market_participation_context)
    technical_readiness = upstream.get("technical_entry_readiness") if isinstance(upstream.get("technical_entry_readiness"), dict) else {}

    source_rows = int(context_overlay_source_diagnostics.get("source_rows_total") or 0)
    active_authorized_rows = int(context_overlay_source_diagnostics.get("active_authorized_rows_total") or 0)
    refresh_target_rows = int(context_overlay_refresh_preview.get("target_rows") or 0)
    refresh_signal_rows = int(context_overlay_refresh_preview.get("signal_rows") or 0)
    positive_watch_pressure = int(context_overlay_pressure.get("positive_or_watch_pressure_count") or 0)
    negative_pressure = int(context_overlay_pressure.get("negative_pressure_count") or 0)
    suppressed_context = int(context_overlay_pressure.get("suppressed_context_count") or 0)
    active_context_watch_rows = int(context_watchlist_intake.get("active_watch_count") or 0)
    breakout_context_watch_rows = int(context_watchlist_intake.get("breakout_watch_count") or 0)
    suppressed_context_watch_rows = int(context_watchlist_intake.get("suppressed_count") or 0)
    context_watchlist_current = bool(context_watchlist_intake.get("current_for_reconcile"))
    context_watchlist_freshness_status = str(context_watchlist_intake.get("context_watchlist_freshness_status") or "").strip()
    technical_precheck_coverage = int(context_watch_technical_precheck.get("technical_symbol_coverage_count") or 0)
    technical_precheck_missing = int(context_watch_technical_precheck.get("missing_technical_symbol_count") or 0)
    technical_precheck_constructive = int(context_watch_technical_precheck.get("constructive_precheck_count") or 0)
    technical_precheck_breakout_like = int(context_watch_technical_precheck.get("breakout_like_precheck_count") or 0)
    technical_precheck_stale = bool(context_watch_technical_precheck.get("technical_features_stale"))
    technical_refresh_issue_symbols = int(context_watch_technical_precheck.get("latest_refresh_issue_symbol_count") or 0)
    technical_input_blockers = (
        context_watch_technical_precheck.get("technical_input_blockers")
        if isinstance(context_watch_technical_precheck.get("technical_input_blockers"), dict)
        else {}
    )
    hard_input_blockers = int(technical_input_blockers.get("hard_blocker_symbol_count") or 0)
    stale_history_blockers = int(technical_input_blockers.get("stale_history_symbol_count") or 0)
    constructive_candidates = int(
        upstream.get("positive_or_constructive_candidate_count")
        or technical_readiness.get("positive_or_constructive_candidate_count")
        or 0
    )
    candidate_rows = int(upstream.get("candidate_rows") or 0)
    technical_state_counts = upstream.get("technical_state_counts") if isinstance(upstream.get("technical_state_counts"), dict) else {}
    technical_ignore_candidate_rows = int(technical_state_counts.get("IGNORE", 0) or technical_state_counts.get("ignore", 0) or 0)
    all_candidates_technical_ignore = bool(candidate_rows > 0 and technical_ignore_candidate_rows >= candidate_rows)
    confirmed_entries = int(technical_readiness.get("confirmed_entry_candidate_count") or 0)
    final_positive_actions = int(action_counts.get("BUY", 0) or 0) + int(action_counts.get("BUY_MORE", 0) or 0)
    final_watch_actions = int(action_counts.get("WATCH", 0) or 0)
    confirmed_lost_downstream = int(upstream.get("confirmed_entry_lost_downstream_count") or 0)
    benchmark_label = str(market_participation_context.get("market_participation_label") or "unknown")

    conversion_steps = [
        {
            "step": "context_sources",
            "count": source_rows,
            "active_authorized_count": active_authorized_rows,
            "status": context_overlay_source_diagnostics.get("status") or "not_loaded",
        },
        {
            "step": "context_watchlist_intake",
            "active_watch_count": active_context_watch_rows,
            "breakout_watch_count": breakout_context_watch_rows,
            "suppressed_count": suppressed_context_watch_rows,
            "status": context_watchlist_intake.get("status") or "not_loaded",
        },
        {
            "step": "context_watch_technical_precheck",
            "technical_symbol_coverage_count": technical_precheck_coverage,
            "missing_technical_symbol_count": technical_precheck_missing,
            "constructive_precheck_count": technical_precheck_constructive,
            "breakout_like_precheck_count": technical_precheck_breakout_like,
            "hard_input_blocker_symbol_count": hard_input_blockers,
            "stale_history_blocker_symbol_count": stale_history_blockers,
            "status": context_watch_technical_precheck.get("status") or "not_loaded",
        },
        {
            "step": "bounded_context_refresh_preview",
            "count": refresh_target_rows,
            "signal_rows": refresh_signal_rows,
            "status": context_overlay_refresh_preview.get("status") or "not_loaded",
        },
        {
            "step": "context_pressure_in_actions",
            "positive_or_watch_count": positive_watch_pressure,
            "negative_count": negative_pressure,
            "suppressed_count": suppressed_context,
            "status": context_overlay_pressure.get("status") or "not_loaded",
        },
        {
            "step": "technical_entry",
            "constructive_candidate_count": constructive_candidates,
            "confirmed_entry_count": confirmed_entries,
            "status": technical_readiness.get("diagnosis") or "not_loaded",
        },
        {
            "step": "final_actions",
            "positive_action_count": final_positive_actions,
            "watch_action_count": final_watch_actions,
            "confirmed_lost_downstream_count": confirmed_lost_downstream,
        },
    ]
    positive_context_entry_blocker = summarize_positive_context_entry_blocker(
        positive_watch_pressure=positive_watch_pressure,
        active_context_watch_rows=active_context_watch_rows,
        context_watchlist_current=context_watchlist_current,
        technical_precheck_coverage=technical_precheck_coverage,
        technical_precheck_missing=technical_precheck_missing,
        technical_precheck_constructive=technical_precheck_constructive,
        technical_precheck_breakout_like=technical_precheck_breakout_like,
        technical_precheck_stale=technical_precheck_stale,
        technical_refresh_issue_symbols=technical_refresh_issue_symbols,
        hard_input_blockers=hard_input_blockers,
        stale_history_blockers=stale_history_blockers,
        constructive_candidates=constructive_candidates,
        confirmed_entries=confirmed_entries,
        final_positive_actions=final_positive_actions,
        confirmed_lost_downstream=confirmed_lost_downstream,
        benchmark_label=benchmark_label,
    )
    source_family_attribution = summarize_context_to_entry_source_family_attribution(
        context_overlay_source_diagnostics=context_overlay_source_diagnostics,
        context_watchlist_intake=context_watchlist_intake,
        context_overlay_pressure=context_overlay_pressure,
        context_overlay_refresh_preview=context_overlay_refresh_preview,
    )

    if source_rows <= 0 and active_authorized_rows <= 0 and refresh_target_rows <= 0 and positive_watch_pressure <= 0 and negative_pressure <= 0:
        status = "no_context_evidence_in_entry_pipeline"
        bottleneck = "context_source_absent_or_not_loaded"
        operator_action = "No context source or pressure evidence is visible in this diagnostic; inspect source availability before changing regime or technical policy."
    elif active_authorized_rows > 0 and active_context_watch_rows <= 0 and refresh_signal_rows <= 0 and positive_watch_pressure <= 0 and negative_pressure <= 0:
        status = "context_source_to_signal_gap"
        bottleneck = "context_refresh_or_conversion"
        operator_action = "Context source rows exist but did not become review-only signals or action pressure; inspect conversion diagnostics, source-family reliability, and sector mapping."
    elif active_context_watch_rows > 0 and positive_watch_pressure <= 0 and negative_pressure <= 0 and refresh_signal_rows <= 0:
        status = "context_watchlist_to_signal_gap"
        bottleneck = "watchlist_signal_refresh_or_action_refresh"
        operator_action = "Context evidence reached persisted watchlist rows, but not signal/action rows; run bounded context-overlay signal refresh or inspect watchlist-to-action conversion."
    elif refresh_signal_rows > 0 and positive_watch_pressure <= 0 and negative_pressure <= 0:
        status = "context_signal_to_action_pressure_gap"
        bottleneck = "action_refresh_or_consolidation"
        operator_action = "Bounded context refresh preview can produce signals, but current actions do not show context pressure; run bounded signal refresh or full advisory before policy tuning."
    elif (
        str(context_watchlist_intake.get("status") or "") == "context_watchlist_rows_exist_for_different_asof"
        and active_context_watch_rows <= 0
        and positive_watch_pressure > 0
    ):
        status = "context_watchlist_asof_mismatch"
        bottleneck = "context_watchlist_date_alignment"
        operator_action = (
            "Context refresh created review-only action pressure for the diagnostic date, but persisted context watchlist rows exist only for a different date; "
            "run the bounded context watchlist reconciliation for this as-of date or rerun advisory on the latest trading date before changing regime/context policy."
        )
    elif positive_watch_pressure > 0 and active_context_watch_rows <= 0 and constructive_candidates <= 0:
        status = "context_direct_refresh_without_persisted_watchlist"
        bottleneck = "full_watchlist_reconciliation_pending"
        operator_action = "Fast context refresh created review-only action pressure before a persisted context watchlist snapshot; run full advisory/watchlist reconciliation before treating this as candidate-generation failure."
    elif positive_watch_pressure > 0 and constructive_candidates <= 0:
        status = "context_pressure_without_constructive_candidates"
        if active_context_watch_rows > 0 and not context_watchlist_current:
            bottleneck = "context_watchlist_reconcile_stale"
            operator_action = "Context pressure exists, but the persisted context-watch snapshot is stale or from an older selection contract; rebuild context-watch rows before refreshing technicals or changing regime/context policy."
        elif active_context_watch_rows > 0 and technical_precheck_coverage <= 0:
            bottleneck = "context_watch_technical_feature_gap"
            operator_action = "Context pressure exists, but active context-watch symbols lack point-in-time technical feature coverage; refresh OHLCV/technical features before changing global regime policy."
        elif hard_input_blockers > 0:
            bottleneck = "context_watch_hard_ohlcv_input_blockers"
            operator_action = (
                "Context pressure exists, but some active context-watch symbols have hard Dhan OHLCV blockers "
                "(no usable history, no-data, or zero-row sync); suppress or repair those rows before changing regime/context policy."
            )
        elif technical_refresh_issue_symbols > 0:
            bottleneck = "context_watch_technical_refresh_failed"
            operator_action = (
                "Context pressure exists, but the latest technical refresh recorded per-symbol issues for active context-watch symbols; "
                "inspect advisory_technical_feature_refresh_status before rerunning full advisory or changing regime/context policy."
            )
        elif technical_precheck_stale:
            bottleneck = "context_watch_technical_feature_stale"
            operator_action = "Context pressure exists, but technical features for context-watch symbols are stale; refresh technical features or rerun advisory before changing regime/context policy."
        elif all_candidates_technical_ignore:
            bottleneck = "candidate_technical_confirmation_missing"
            operator_action = (
                "Rule-engine candidates exist, but every candidate has technical_state=IGNORE. "
                "Inspect technical-engine trigger thresholds, candidate technical handoff, and Dhan OHLCV feature quality before rerunning candidate generation or loosening regime/context gates."
            )
        elif technical_precheck_constructive > 0:
            bottleneck = "candidate_generation_gap_after_technical_precheck"
            operator_action = "Context-watch symbols have constructive technical precheck evidence, but no rule-engine candidates are visible; rerun full advisory/candidate generation rather than loosening global regime gates."
        else:
            bottleneck = "watch_universe_or_candidate_generation"
            operator_action = "Context pressure exists but no constructive technical candidates are visible; inspect watchlist intake and candidate generation rather than global regime labels."
    elif positive_watch_pressure > 0 and confirmed_entries <= 0:
        status = "context_pressure_waiting_for_technical_entry"
        bottleneck = "technical_entry_confirmation"
        operator_action = "Context pressure reached watch/action rows, but technical entry has not confirmed; research technical-entry thresholds without granting context-only BUY authority."
    elif confirmed_entries > 0 and final_positive_actions <= 0:
        status = "confirmed_entry_lost_after_context_pipeline"
        bottleneck = "downstream_risk_portfolio_consolidation"
        operator_action = "Confirmed technical entries exist but no final BUY/BUY_MORE survived; inspect risk, lifecycle, conflict, and action consolidation before touching regime/context policy."
    elif final_positive_actions > 0:
        status = "context_pipeline_has_positive_actions"
        bottleneck = "none"
        operator_action = "Positive actions exist; use execution/risk readiness checks before any broker review."
    else:
        status = "context_pipeline_mixed_or_unclassified"
        bottleneck = "mixed"
        operator_action = "Context pipeline evidence is mixed; inspect the step counts before changing global regime or technical thresholds."

    preview_asof = (
        context_overlay_refresh_preview.get("requested_asof_date")
        or context_overlay_refresh_preview.get("asof_date")
        or (
            context_overlay_refresh_preview.get("diagnostics", {}).get("asof_date")
            if isinstance(context_overlay_refresh_preview.get("diagnostics"), dict)
            else None
        )
        or context_watchlist_intake.get("asof_date")
    )
    signal_refresh_command = str(
        context_overlay_refresh_preview.get("recommended_command")
        or (
            f"python -m advisory.signal_refresh --from-context-overlays --asof-date {pd.to_datetime(preview_asof, utc=True, errors='coerce').date().isoformat()} --limit 50 --format text"
            if preview_asof is not None and not pd.isna(pd.to_datetime(preview_asof, utc=True, errors="coerce"))
            else "python -m advisory.signal_refresh --from-context-overlays --limit 50 --format text"
        )
    )
    next_commands: list[dict[str, str]] = []
    if status in {"context_direct_refresh_without_persisted_watchlist", "context_watchlist_asof_mismatch"}:
        verify_command = recommendation_diagnostics_command(preview_asof, output_format="text")
        next_commands.append(
            {
                "command": context_watchlist_reconcile_command(preview_asof),
                "purpose": "Persist context-overlay watchlist rows for the diagnostic date so news/macro/bhavcopy pressure is visible before full advisory reconciliation.",
                "verify_command": verify_command,
                "expected_after_success": "context_to_entry_pipeline.active_context_watchlist_rows > 0 or context_watchlist_intake.status no longer reports missing/different-date rows.",
                "authority": "watchlist_only_no_portfolio_no_broker",
            }
        )
        next_commands.append(
            {
                "command": bounded_authoritative_reconciliation_command(preview_asof),
                "purpose": "Regenerate authoritative rule candidates, risk, portfolio, lifecycle, and consolidated actions from already-current inputs after context-watchlist reconciliation.",
                "verify_command": verify_command,
                "expected_after_success": "candidate_source_availability has same-date candidate rows and primary_no_buy_cause is no longer candidate_source_stale_for_asof.",
                "authority": "authoritative_advisory_no_direct_broker_execution",
            }
        )
    elif status in {"context_watchlist_to_signal_gap", "context_signal_to_action_pressure_gap"}:
        verify_command = recommendation_diagnostics_command(preview_asof, output_format="text")
        next_commands.append(
            {
                "command": signal_refresh_command,
                "purpose": "Persist review-only context-overlay Action Queue rows from existing context-watchlist evidence before the long advisory rerun.",
                "verify_command": verify_command,
                "expected_after_success": "context_overlay_pressure.positive_or_watch_pressure_count or negative_pressure_count is non-zero.",
                "authority": "review_only_action_queue_no_portfolio_no_broker",
            }
        )
    elif bottleneck == "context_watchlist_reconcile_stale":
        stale_followup_date = context_watch_technical_precheck.get("latest_trading_day") or preview_asof
        stale_followup_ts = pd.to_datetime(stale_followup_date, utc=True, errors="coerce")
        stale_reconcile_asof = None if pd.isna(stale_followup_ts) else stale_followup_ts
        verify_command = recommendation_diagnostics_command(stale_reconcile_asof or preview_asof, output_format="text")
        stale_advisory_command = bounded_authoritative_reconciliation_command(
            stale_followup_ts if not pd.isna(stale_followup_ts) else None
        )
        next_commands.append(
            {
                "command": context_watchlist_reconcile_command(stale_reconcile_asof or preview_asof),
                "purpose": "Rebuild stale context-overlay watchlist rows before refreshing technicals for the old watchlist symbols.",
                "verify_command": verify_command,
                "expected_after_success": "context_watchlist_intake.current_for_reconcile is true and context_watchlist_freshness_status is current.",
                "authority": "watchlist_only_no_portfolio_no_broker",
            }
        )
        next_commands.append(
            {
                "command": stale_advisory_command,
                "purpose": "Regenerate authoritative rule candidates and consolidated actions from already-current inputs after context-watchlist reconciliation.",
                "verify_command": verify_command,
                "expected_after_success": "candidate_source_availability has same-date candidate rows or context_to_entry_pipeline moves past stale watchlist reconciliation.",
                "authority": "authoritative_advisory_no_direct_broker_execution",
            }
        )
    elif bottleneck in {"context_watch_technical_feature_gap", "context_watch_technical_feature_stale", "context_watch_technical_refresh_failed", "context_watch_hard_ohlcv_input_blockers"}:
        targeted_command = str(context_watch_technical_precheck.get("targeted_repair_command") or "").strip()
        command = targeted_command or str(context_watch_technical_precheck.get("recommended_command") or "").strip()
        followup_date = (
            context_watch_technical_precheck.get("latest_trading_day")
            or context_watch_technical_precheck.get("asof_date")
            or preview_asof
        )
        followup_ts = pd.to_datetime(followup_date, utc=True, errors="coerce")
        advisory_followup_command = bounded_authoritative_reconciliation_command(
            followup_ts if not pd.isna(followup_ts) else None
        )
        if bottleneck == "context_watch_hard_ohlcv_input_blockers":
            next_commands.append(
                {
                    "command": context_watchlist_reconcile_command(followup_ts if not pd.isna(followup_ts) else preview_asof),
                    "purpose": "Reconcile context-watch rows so hard OHLCV-blocked symbols are suppressed until usable Dhan daily history exists.",
                    "verify_command": recommendation_diagnostics_command(preview_asof, output_format="text"),
                    "expected_after_success": "context_watch_technical_precheck.technical_input_blockers.hard_blocker_symbol_count is zero for active context-watch rows or those symbols move to suppressed context-watch audit rows.",
                    "authority": "watchlist_only_no_portfolio_no_broker",
                }
            )
        elif command:
            targeted_count = int(context_watch_technical_precheck.get("targeted_repair_symbol_count") or 0)
            next_commands.append(
                {
                    "command": command,
                    "purpose": (
                        f"Refresh point-in-time technical features for {targeted_count} context-watch symbols with missing, stale, or failed technical inputs before interpreting no-BUY as regime or threshold behavior."
                        if targeted_command and targeted_count > 0
                        else "Refresh point-in-time technical features for active context-watch symbols before interpreting no-BUY as regime or threshold behavior."
                    ),
                    "verify_command": recommendation_diagnostics_command(preview_asof, output_format="text"),
                    "expected_after_success": "context_watch_technical_precheck.technical_symbol_coverage_count increases, latest_refresh_issue_symbol_count is zero, and technical_features_stale is false.",
                    "authority": "data_feature_refresh_no_portfolio_no_broker",
                }
            )
        next_commands.append(
            {
                "command": advisory_followup_command,
                "purpose": "Regenerate authoritative rule candidates and consolidated actions from already-current inputs after context-watch technical features are fresh.",
                "verify_command": recommendation_diagnostics_command(preview_asof, output_format="text"),
                "expected_after_success": "candidate_source_availability has same-date candidate rows or context_to_entry_pipeline moves past the technical-feature bottleneck.",
                "authority": "authoritative_advisory_no_direct_broker_execution",
            }
        )
    elif status == "context_pressure_waiting_for_technical_entry":
        next_commands.append(
            {
                "command": recommendation_diagnostics_command(preview_asof, output_format="json"),
                "purpose": "Inspect constructive candidates and technical-entry confirmation before changing context or regime policy.",
                "authority": "read_only_diagnostic",
            }
        )

    return {
        "status": status,
        "bottleneck": bottleneck,
        "market_participation_label": benchmark_label,
        "context_source_rows": source_rows,
        "active_authorized_context_rows": active_authorized_rows,
        "refresh_target_rows": refresh_target_rows,
        "refresh_signal_rows": refresh_signal_rows,
        "active_context_watchlist_rows": active_context_watch_rows,
        "breakout_context_watchlist_rows": breakout_context_watch_rows,
        "suppressed_context_watchlist_rows": suppressed_context_watch_rows,
        "context_watch_technical_precheck_status": context_watch_technical_precheck.get("status"),
        "context_watchlist_current": bool(context_watchlist_current),
        "context_watchlist_freshness_status": context_watchlist_freshness_status or None,
        "context_watch_technical_coverage_count": technical_precheck_coverage,
        "context_watch_technical_missing_count": technical_precheck_missing,
        "context_watch_technical_stale": bool(technical_precheck_stale),
        "context_watch_technical_refresh_issue_symbol_count": int(technical_refresh_issue_symbols),
        "context_watch_hard_input_blocker_symbol_count": hard_input_blockers,
        "context_watch_stale_history_blocker_symbol_count": stale_history_blockers,
        "context_watch_constructive_precheck_count": technical_precheck_constructive,
        "context_watch_breakout_like_precheck_count": technical_precheck_breakout_like,
        "positive_or_watch_context_pressure_count": positive_watch_pressure,
        "negative_context_pressure_count": negative_pressure,
        "suppressed_context_count": suppressed_context,
        "constructive_candidate_count": constructive_candidates,
        "candidate_rows": candidate_rows,
        "technical_ignore_candidate_rows": technical_ignore_candidate_rows,
        "all_candidates_technical_ignore": all_candidates_technical_ignore,
        "confirmed_entry_candidate_count": confirmed_entries,
        "final_positive_action_count": final_positive_actions,
        "final_watch_action_count": final_watch_actions,
        "confirmed_entry_lost_downstream_count": confirmed_lost_downstream,
        "positive_context_entry_blocker": positive_context_entry_blocker,
        "source_family_attribution": source_family_attribution,
        "conversion_steps": conversion_steps,
        "operator_action": operator_action,
        "next_commands": next_commands,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "explain where context/news/macro evidence stops before becoming final entry actions",
        },
    }


def summarize_action_queue_authority_state(
    *,
    row_count: int,
    action_source_counts: Counter[str] | dict[str, int] | None = None,
    candidate_source_availability: dict[str, Any] | None = None,
    broker_candidate_count: int = 0,
) -> dict[str, Any]:
    source_counts = {
        str(key or "unknown"): int(value or 0)
        for key, value in (action_source_counts or {}).items()
        if int(value or 0) > 0
    }
    candidate_source_availability = candidate_source_availability if isinstance(candidate_source_availability, dict) else {}
    candidate_status = str(candidate_source_availability.get("status") or "").strip()
    candidate_rows_for_asof = int(candidate_source_availability.get("candidate_rows_for_asof") or 0)
    latest_candidate_rows = int(candidate_source_availability.get("latest_candidate_rows") or 0)
    latest_candidate_asof_date = candidate_source_availability.get("latest_candidate_asof_date")
    all_signal_refresh = bool(row_count) and source_counts == {"signal_refresh": int(row_count)}
    authoritative_candidates_current = candidate_status == "candidate_rows_available_for_asof" and candidate_rows_for_asof > 0

    base = {
        "row_count": int(row_count),
        "action_source_counts": source_counts,
        "candidate_source_status": candidate_status or None,
        "candidate_rows_for_asof": candidate_rows_for_asof,
        "latest_candidate_rows": latest_candidate_rows,
        "latest_candidate_asof_date": latest_candidate_asof_date,
        "broker_candidate_count": int(broker_candidate_count or 0),
        "portfolio_authority": "none",
        "broker_execution_allowed": False,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "separate fast review-only Action Queue visibility from authoritative full-advisory reconciliation",
        },
    }
    if row_count <= 0:
        return {
            **base,
            "status": "no_action_queue_rows",
            "authority_level": "none",
            "full_advisory_required": True,
            "operator_action": "No Action Queue rows exist for this date; run all_advisory.sh before interpreting no-BUY causes.",
        }
    if all_signal_refresh and not authoritative_candidates_current:
        return {
            **base,
            "status": "fast_review_only_queue_authoritative_advisory_missing",
            "authority_level": "fast_review_only_visibility",
            "full_advisory_required": True,
            "operator_action": (
                "Current Action Queue rows come only from fast signal-refresh evidence, while same-date rule-engine candidates are missing or stale. "
                "Treat no-BUY as an authoritative advisory freshness issue, not as a regime, LLM, or technical-threshold conclusion."
            ),
        }
    if all_signal_refresh:
        return {
            **base,
            "status": "fast_review_only_queue",
            "authority_level": "fast_review_only_visibility",
            "full_advisory_required": True,
            "operator_action": (
                "Current Action Queue rows come only from signal-refresh evidence. They are useful for watch/de-risk visibility, "
                "but full advisory is still required before portfolio sizing or broker-candidate interpretation."
            ),
        }
    if authoritative_candidates_current:
        return {
            **base,
            "status": "authoritative_advisory_rows_present",
            "authority_level": "full_advisory_reconciled",
            "full_advisory_required": False,
            "operator_action": "Same-date rule-engine candidates are present; no-BUY analysis can inspect technical, context, risk, and portfolio gates.",
        }
    return {
        **base,
        "status": "mixed_or_stale_action_queue",
        "authority_level": "mixed_or_stale",
        "full_advisory_required": True,
        "operator_action": (
            "Action Queue rows are mixed or candidate freshness is unclear; verify candidate-source availability and rerun all_advisory.sh before policy tuning."
        ),
    }


def summarize_layered_underparticipation_attribution(
    *,
    positive_count: int,
    action_counts: Counter[str] | dict[str, int] | None = None,
    upstream: dict[str, Any] | None = None,
    candidate_source_availability: dict[str, Any] | None = None,
    context_overlay_pressure: dict[str, Any] | None = None,
    context_overlay_source_diagnostics: dict[str, Any] | None = None,
    context_overlay_refresh_preview: dict[str, Any] | None = None,
    context_overlay_signal_refresh_state: dict[str, Any] | None = None,
    context_to_entry_pipeline: dict[str, Any] | None = None,
    signal_quality_attribution: dict[str, Any] | None = None,
    market_context_blocks: dict[str, Any] | None = None,
    market_participation_context: dict[str, Any] | None = None,
    primary_no_buy_cause: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attribute missing BUYs across layers instead of blaming one regime label."""

    action_counts = action_counts if isinstance(action_counts, (Counter, dict)) else {}
    upstream = upstream if isinstance(upstream, dict) else {}
    candidate_source_availability = candidate_source_availability if isinstance(candidate_source_availability, dict) else {}
    context_overlay_pressure = context_overlay_pressure if isinstance(context_overlay_pressure, dict) else {}
    context_overlay_source_diagnostics = context_overlay_source_diagnostics if isinstance(context_overlay_source_diagnostics, dict) else {}
    context_overlay_refresh_preview = context_overlay_refresh_preview if isinstance(context_overlay_refresh_preview, dict) else {}
    context_overlay_signal_refresh_state = (
        context_overlay_signal_refresh_state if isinstance(context_overlay_signal_refresh_state, dict) else {}
    )
    context_to_entry_pipeline = context_to_entry_pipeline if isinstance(context_to_entry_pipeline, dict) else {}
    context_overlay_signal_refresh_state = (
        context_overlay_signal_refresh_state if isinstance(context_overlay_signal_refresh_state, dict) else {}
    )
    signal_quality_attribution = summarize_signal_quality_attribution(signal_quality_attribution)
    market_context_blocks = market_context_blocks if isinstance(market_context_blocks, dict) else {}
    market_participation_context = summarize_market_participation_context(market_participation_context)
    primary_no_buy_cause = primary_no_buy_cause if isinstance(primary_no_buy_cause, dict) else {}
    source_family_attribution = (
        context_to_entry_pipeline.get("source_family_attribution")
        if isinstance(context_to_entry_pipeline.get("source_family_attribution"), dict)
        else {}
    )

    benchmark_label = str(market_participation_context.get("market_participation_label") or "unknown")
    candidate_status = str(candidate_source_availability.get("status") or "")
    context_source_status = str(context_overlay_source_diagnostics.get("status") or "")
    context_pressure_status = str(context_overlay_pressure.get("status") or "")
    primary_code = str(primary_no_buy_cause.get("code") or "")
    technical_readiness = upstream.get("technical_entry_readiness") if isinstance(upstream.get("technical_entry_readiness"), dict) else {}
    constructive = int(upstream.get("positive_or_constructive_candidate_count") or technical_readiness.get("positive_or_constructive_candidate_count") or 0)
    confirmed = int(technical_readiness.get("confirmed_entry_candidate_count") or 0)
    context_preview_signals = int(context_overlay_refresh_preview.get("signal_rows") or 0)
    context_signal_refresh_current = bool(context_overlay_signal_refresh_state.get("current_for_preview"))
    persisted_context_signals = int(context_overlay_signal_refresh_state.get("state_signal_rows") or 0)
    active_context_sources = int(context_overlay_source_diagnostics.get("active_authorized_rows_total") or 0)
    context_watch_pressure = int(context_overlay_pressure.get("positive_or_watch_pressure_count") or 0)
    signal_quality_blocked = int(signal_quality_attribution.get("benchmark_or_attribution_blocked_count") or 0)
    hard_market_blocks = int(market_context_blocks.get("blocked_count") or 0)
    watch_count = int(action_counts.get("WATCH", 0) or 0)

    if int(positive_count or 0) > 0:
        attribution = "participating_with_positive_actions"
        operator_action = "BUY/BUY_MORE rows exist; use broker-candidate and lifecycle diagnostics rather than missing-BUY attribution."
        severity = "info"
    elif candidate_status in {"candidate_rows_missing_for_asof_latest_exists", "candidate_rows_missing", "candidate_source_unavailable", "candidate_source_availability_failed"}:
        attribution = "candidate_source_gap"
        readiness_status = str(primary_no_buy_cause.get("readiness_status") or "")
        readiness_action = str(primary_no_buy_cause.get("readiness_operator_action") or "").strip()
        if readiness_status == "bounded_refresh_before_full_advisory" and readiness_action:
            operator_action = f"{readiness_action} Underlying attribution remains candidate-source freshness; regenerate rule-engine candidates before interpreting missing BUYs as regime, threshold, context, or LLM behavior."
        else:
            operator_action = "Regenerate rule-engine candidates before interpreting missing BUYs as regime, threshold, context, or LLM behavior."
        severity = "blocker"
    elif context_preview_signals > 0 and not context_signal_refresh_current:
        attribution = "bounded_context_refresh_pending"
        operator_action = "Run the bounded review-only context-overlay signal refresh first, then rerun full advisory for authoritative reconciliation."
        severity = "actionable"
    elif benchmark_label == "benchmark_uptrend" and signal_quality_blocked > 0:
        attribution = "context_signal_quality_attribution_blocked_in_rising_market"
        operator_action = "The benchmark is rising, but context signal-quality evidence is blocked by market beta or missing benchmark attribution; run rolling-window split diagnostics before changing regime or context policy."
        severity = "research_blocker"
    elif benchmark_label == "benchmark_uptrend" and constructive > 0 and confirmed == 0:
        attribution = "technical_confirmation_gap_in_rising_market"
        operator_action = "The benchmark is rising and constructive candidates exist, but technical entry is not confirmed; research technical trigger thresholds before touching global regime policy."
        severity = "actionable"
    elif benchmark_label == "benchmark_uptrend" and active_context_sources > 0 and context_watch_pressure == 0:
        attribution = "context_source_to_signal_conversion_gap_in_rising_market"
        operator_action = "Context source rows exist during a rising benchmark but did not become watch/de-risk pressure; inspect context-overlay conversion diagnostics and sector/source reliability."
        severity = "actionable"
    elif benchmark_label == "benchmark_uptrend" and context_watch_pressure > 0 and watch_count > 0:
        attribution = "watch_pressure_waiting_for_entry_trigger_in_rising_market"
        operator_action = "Context/news/macro pressure reached watch rows during a rising benchmark, but no BUY trigger fired; validate technical entry timing and downstream consolidation."
        severity = "info"
    elif int(upstream.get("confirmed_entry_lost_downstream_count") or 0) > 0:
        attribution = "downstream_consolidation_gap"
        operator_action = "Confirmed technical candidates existed but were downgraded downstream; inspect action/risk/portfolio/lifecycle consolidation before changing regime/context policy."
        severity = "review"
    elif hard_market_blocks > 0:
        attribution = "hard_market_or_macro_block"
        operator_action = "Hard market/macro blocks are present; inspect whether they are true hard-risk evidence rather than a single broad regime label."
        severity = "review"
    elif benchmark_label == "benchmark_uptrend":
        attribution = "unexplained_underparticipation_in_rising_market"
        operator_action = "The benchmark is rising but the diagnostic did not find a single dominant layer; inspect source freshness, technical confirmation, context conversion, and consolidation in order."
        severity = "review"
    else:
        attribution = "no_rising_market_underparticipation_signal"
        operator_action = "Broad benchmark participation does not indicate a clear rising-market underparticipation issue; continue layer-specific diagnostics."
        severity = "info"

    return {
        "status": attribution,
        "severity": severity,
        "benchmark_participation_label": benchmark_label,
        "primary_no_buy_cause_code": primary_code,
        "positive_action_count": int(positive_count or 0),
        "watch_count": watch_count,
        "constructive_candidate_count": constructive,
        "confirmed_entry_candidate_count": confirmed,
        "context_overlay_source_status": context_source_status,
        "context_overlay_pressure_status": context_pressure_status,
        "active_context_overlay_source_rows": active_context_sources,
        "context_overlay_preview_signal_rows": context_preview_signals,
        "context_overlay_signal_refresh_current": context_signal_refresh_current,
        "context_overlay_persisted_signal_rows": persisted_context_signals,
        "context_overlay_watch_pressure_count": context_watch_pressure,
        "signal_quality_attribution_status": signal_quality_attribution.get("status"),
        "signal_quality_attribution_blocked_count": signal_quality_blocked,
        "hard_market_context_block_count": hard_market_blocks,
        "operator_action": operator_action,
        "recommended_order": [
            "candidate_source_freshness",
            "bounded_context_refresh",
            "technical_entry_confirmation",
            "context_source_to_signal_conversion",
            "downstream_action_risk_portfolio_lifecycle_consolidation",
            "hard_macro_or_market_context_evidence",
        ],
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "attribute missing BUY rows across market, sector/context, technical, and consolidation layers before changing global regime policy",
            "global_regime_policy_effect": "diagnostic_only_do_not_tune_first",
        },
    }


def summarize_context_overlay_source_availability(diagnostics: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(diagnostics, dict):
        return {
            "status": "not_loaded",
            "operator_action": "Context-overlay source-table diagnostics were not loaded for this in-memory diagnostic call.",
            "policy_boundary": {
                "report_mode": "read_only_diagnostic",
                "portfolio_authority": "none",
                "broker_execution_allowed": False,
                "decision_use": "explain whether context-overlay source rows existed before diagnosing absent context pressure",
            },
        }
    sources = diagnostics.get("sources") if isinstance(diagnostics.get("sources"), list) else []
    compact_sources: list[dict[str, Any]] = []
    for item in sources[:10]:
        if not isinstance(item, dict):
            continue
        compact_sources.append(
            {
                "source": item.get("source"),
                "table": item.get("table"),
                "status": item.get("status"),
                "exists": bool(item.get("exists")),
                "required_columns_present": item.get("required_columns_present"),
                "row_count": int(item.get("row_count") or 0),
                "active_authorized_rows": int(item.get("active_authorized_rows") or 0),
                "positive_watch_rows": int(item.get("positive_watch_rows") or 0),
                "negative_rows": int(item.get("negative_rows") or 0),
                "latest_date": item.get("latest_date"),
                "missing_required_columns": item.get("missing_required_columns") or [],
            }
        )
    source_rows_total = int(diagnostics.get("source_rows_total") or 0)
    active_authorized_rows_total = int(diagnostics.get("active_authorized_rows_total") or 0)
    source_status = str(diagnostics.get("source_status") or "").strip() or "unknown"
    if source_rows_total <= 0:
        status = "no_context_overlay_source_rows"
        operator_action = (
            "No context-overlay source rows existed for this as-of date; refresh compact announcement, bhavcopy, "
            "exchange, macro, and news-theme overlays before treating missing context pressure as a model issue."
        )
    elif active_authorized_rows_total <= 0:
        status = "context_overlay_rows_exist_but_none_active_authorized"
        operator_action = (
            "Context-overlay rows exist, but none are active watchlist-pressure rows; inspect source status, production_status, "
            "authority_scope, and reliability suppression before expecting watch/de-risk pressure."
        )
    else:
        status = "context_overlay_source_rows_available"
        operator_action = (
            "Context-overlay source rows are available; if consolidated actions show no context pressure, inspect signal-refresh "
            "context-overlay matching, source-family reliability suppression, and watchlist/portfolio target coverage."
        )
    return {
        "status": status,
        "source_status": source_status,
        "empty_reason": diagnostics.get("empty_reason"),
        "asof_date": None if diagnostics.get("asof_date") is None else str(diagnostics.get("asof_date")),
        "source_count": int(diagnostics.get("source_count") or len(compact_sources)),
        "available_source_count": int(diagnostics.get("available_source_count") or 0),
        "source_rows_total": source_rows_total,
        "active_authorized_rows_total": active_authorized_rows_total,
        "sources": compact_sources,
        "watchlist": diagnostics.get("watchlist") if isinstance(diagnostics.get("watchlist"), dict) else {},
        "portfolio": diagnostics.get("portfolio") if isinstance(diagnostics.get("portfolio"), dict) else {},
        "operator_action": operator_action,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "explain whether context-overlay source rows existed before diagnosing absent context pressure",
        },
    }


def summarize_context_watchlist_intake(rows: pd.DataFrame | None) -> dict[str, Any]:
    """Summarize persisted context-overlay watchlist rows without granting authority.

    This is the missing middle of the source -> signal -> action diagnostic path:
    context source rows can become active watch rows, breakout-watch pressure, or
    inactive suppression/audit rows before they ever appear in final actions.
    """

    if not isinstance(rows, pd.DataFrame):
        return {
            "status": "not_loaded",
            "operator_action": "Context watchlist intake rows were not loaded for this in-memory diagnostic call.",
            "policy_boundary": {
                "report_mode": "read_only_diagnostic",
                "portfolio_authority": "none",
                "broker_execution_allowed": False,
                "decision_use": "explain whether context evidence reached persisted watchlist rows",
            },
        }
    empty_confirmation_plan_summary = {
        "plan_count": 0,
        "entry_gate_counts": {},
        "wait_for_counts": {},
        "candidate_state_counts": {},
        "samples": [],
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "explain which technical/risk/lifecycle gates context watch rows are waiting for",
        },
    }
    if rows.empty:
        return {
            "status": "no_context_watchlist_rows",
            "row_count": 0,
            "active_watch_count": 0,
            "breakout_watch_count": 0,
            "suppressed_count": 0,
            "watch_status_counts": {},
            "candidate_state_counts": {},
            "policy_effect_counts": {},
            "context_source_counts": {},
            "reliability_counts": {},
            "technical_confirmation_plan_summary": empty_confirmation_plan_summary,
            "samples": [],
            "operator_action": "No persisted context-overlay watchlist rows were found for this as-of date; inspect context source availability and watchlist build timing.",
            "policy_boundary": {
                "report_mode": "read_only_diagnostic",
                "portfolio_authority": "none",
                "broker_execution_allowed": False,
                "decision_use": "explain whether context evidence reached persisted watchlist rows",
            },
        }

    frame = rows.copy()
    for column in [
        "symbol",
        "candidate_state",
        "current_state",
        "watch_status",
        "context_source",
        "context_policy_effect",
        "context_reliability_classification",
        "context_class_reliability_classification",
        "context_authority_scope",
        "watch_reasons_json",
        "load_ts",
    ]:
        if column not in frame.columns:
            frame[column] = pd.NA
    if "watch_enabled" not in frame.columns:
        frame["watch_enabled"] = False
    frame["symbol"] = frame["symbol"].astype("string").str.strip().str.upper()
    frame["candidate_state"] = frame["candidate_state"].astype("string").str.strip().str.upper()
    frame["current_state"] = frame["current_state"].astype("string").str.strip().str.upper()
    frame["watch_status"] = frame["watch_status"].astype("string").str.strip().str.lower()
    frame["context_source"] = frame["context_source"].astype("string").str.strip()
    frame["context_policy_effect"] = frame["context_policy_effect"].astype("string").str.strip()
    frame["context_reliability_classification"] = frame["context_reliability_classification"].astype("string").str.strip()
    frame["context_class_reliability_classification"] = frame["context_class_reliability_classification"].astype("string").str.strip()
    frame["context_authority_scope"] = frame["context_authority_scope"].astype("string").str.strip()
    active_mask = frame["watch_enabled"].map(_boolish) & ~frame["current_state"].isin(["REJECT", "ABSTAIN"])
    breakout_mask = active_mask & frame["candidate_state"].eq("WATCH_BREAKOUT")
    suppressed_mask = (
        frame["current_state"].isin(["REJECT", "ABSTAIN"])
        | frame["watch_status"].isin(["rejected", "abstained"])
        | frame["context_policy_effect"].str.startswith("suppress_", na=False)
    )
    identity_suppressed = frame["context_policy_effect"].eq("suppress_context_identity_unresolved_no_trade_authority")
    class_suppressed = frame["context_policy_effect"].eq("suppress_context_class_watch_only_no_sell_authority")
    negative_suppressed = frame["context_policy_effect"].eq("suppress_context_watch_only_no_sell_authority")
    active_watch_count = int(active_mask.sum())
    breakout_watch_count = int(breakout_mask.sum())
    suppressed_count = int(suppressed_mask.sum())
    latest_load_ts = pd.to_datetime(frame["load_ts"], utc=True, errors="coerce").max()
    latest_load_ts = None if pd.isna(latest_load_ts) else latest_load_ts
    technical_confirmation_plan_summary = summarize_context_watch_confirmation_plans(frame.loc[active_mask])
    active_builder_version_count = 0
    if active_watch_count > 0 and "watch_reasons_json" in frame.columns:
        for item in frame.loc[active_mask].to_dict(orient="records"):
            reason = _first_context_watch_reason_payload(item.get("watch_reasons_json"))
            if _text(reason.get("context_watchlist_builder_version")) == CONTEXT_OVERLAY_WATCHLIST_BUILDER_VERSION:
                active_builder_version_count += 1

    if breakout_watch_count > 0:
        status = "context_watchlist_breakout_pressure"
        operator_action = "Context evidence reached WATCH_BREAKOUT rows; inspect technical confirmation and downstream risk/lifecycle gates before expecting BUY actions."
    elif active_watch_count > 0:
        status = "context_watchlist_active_watch_pressure"
        operator_action = "Context evidence reached active watchlist rows but not breakout-watch rows; inspect technical-entry confirmation before changing global regime policy."
    elif suppressed_count > 0:
        status = "context_watchlist_suppressed_only"
        operator_action = "Context evidence was persisted only as suppressed/rejected watchlist audit rows; inspect identity, reliability, and negative-context suppression before treating context as absent."
    else:
        status = "context_watchlist_rows_mixed_or_inactive"
        operator_action = "Context watchlist rows exist but are not clearly active or suppressed; inspect samples and schema before changing context policy."

    samples: list[dict[str, Any]] = []
    sample_frame = frame.sort_values(
        by=["watch_enabled", "candidate_state", "symbol"],
        ascending=[False, True, True],
        kind="mergesort",
    )
    for item in sample_frame.head(10).to_dict(orient="records"):
        samples.append(
            {
                "symbol": _text(item.get("symbol")),
                "candidate_state": _text(item.get("candidate_state")),
                "current_state": _text(item.get("current_state")),
                "watch_status": _text(item.get("watch_status")),
                "watch_enabled": _boolish(item.get("watch_enabled")),
                "context_source": _text(item.get("context_source")),
                "context_policy_effect": _text(item.get("context_policy_effect")),
                "context_reliability_classification": _text(item.get("context_reliability_classification")),
                "context_class_reliability_classification": _text(item.get("context_class_reliability_classification")),
                "context_authority_scope": _text(item.get("context_authority_scope")),
            }
        )

    return {
        "status": status,
        "row_count": int(len(frame)),
        "active_watch_count": active_watch_count,
        "active_builder_version_count": int(active_builder_version_count),
        "active_stale_builder_version_count": int(max(0, active_watch_count - active_builder_version_count)),
        "context_watchlist_builder_version": CONTEXT_OVERLAY_WATCHLIST_BUILDER_VERSION,
        "breakout_watch_count": breakout_watch_count,
        "suppressed_count": suppressed_count,
        "latest_load_ts": None if latest_load_ts is None else latest_load_ts.isoformat(),
        "identity_suppressed_count": int(identity_suppressed.sum()),
        "class_suppressed_count": int(class_suppressed.sum()),
        "negative_context_suppressed_count": int(negative_suppressed.sum()),
        "watch_status_counts": dict(Counter(_text(value) or "missing" for value in frame["watch_status"]).most_common()),
        "candidate_state_counts": dict(Counter(_text(value) or "missing" for value in frame["candidate_state"]).most_common()),
        "policy_effect_counts": dict(Counter(_text(value) or "missing" for value in frame["context_policy_effect"]).most_common()),
        "context_source_counts": dict(Counter(_text(value) or "missing" for value in frame["context_source"]).most_common()),
        "reliability_counts": dict(Counter(_text(value) or "missing" for value in frame["context_reliability_classification"]).most_common()),
        "technical_confirmation_plan_summary": technical_confirmation_plan_summary,
        "samples": samples,
        "operator_action": operator_action,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "explain whether context evidence reached persisted watchlist rows",
        },
    }


def _first_context_watch_reason_payload(value: Any) -> dict[str, Any]:
    reasons = _jsonish(value, [])
    if isinstance(reasons, dict):
        return reasons
    if isinstance(reasons, list):
        for item in reasons:
            if isinstance(item, dict):
                return item
    return {}


def summarize_context_watch_confirmation_plans(rows: pd.DataFrame | None) -> dict[str, Any]:
    if not isinstance(rows, pd.DataFrame) or rows.empty or "watch_reasons_json" not in rows.columns:
        return {
            "plan_count": 0,
            "entry_gate_counts": {},
            "wait_for_counts": {},
            "candidate_state_counts": {},
            "samples": [],
            "policy_boundary": {
                "report_mode": "read_only_diagnostic",
                "portfolio_authority": "none",
                "broker_execution_allowed": False,
                "decision_use": "explain which technical/risk/lifecycle gates context watch rows are waiting for",
            },
        }
    entry_gate_counts: Counter[str] = Counter()
    wait_for_counts: Counter[str] = Counter()
    candidate_state_counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    plan_count = 0
    for item in rows.to_dict(orient="records"):
        reason = _first_context_watch_reason_payload(item.get("watch_reasons_json"))
        plan = reason.get("technical_confirmation_plan") if isinstance(reason, dict) else None
        if not isinstance(plan, dict) or not plan:
            continue
        plan_count += 1
        entry_gate = _text(plan.get("entry_gate")) or "missing"
        candidate_state = _text(plan.get("context_candidate_state") or item.get("candidate_state")) or "missing"
        entry_gate_counts[entry_gate] += 1
        candidate_state_counts[candidate_state] += 1
        wait_for = plan.get("wait_for")
        if isinstance(wait_for, list):
            for value in wait_for:
                wait_for_counts[_text(value) or "missing"] += 1
        elif _text(wait_for):
            wait_for_counts[_text(wait_for) or "missing"] += 1
        if len(samples) < 10:
            samples.append(
                {
                    "symbol": _text(item.get("symbol")),
                    "context_source": _text(item.get("context_source")),
                    "candidate_state": candidate_state,
                    "entry_gate": entry_gate,
                    "wait_for": [_text(value) for value in wait_for if _text(value)] if isinstance(wait_for, list) else ([_text(wait_for)] if _text(wait_for) else []),
                    "operator_summary": _text(plan.get("operator_summary")),
                    "broker_execution_allowed": bool(plan.get("broker_execution_allowed")) if plan.get("broker_execution_allowed") is not None else False,
                    "portfolio_authority": _text(plan.get("portfolio_authority")) or "none",
                    "full_advisory_required": bool(plan.get("full_advisory_required")) if plan.get("full_advisory_required") is not None else True,
                }
            )
    return {
        "plan_count": int(plan_count),
        "entry_gate_counts": dict(entry_gate_counts.most_common()),
        "wait_for_counts": dict(wait_for_counts.most_common()),
        "candidate_state_counts": dict(candidate_state_counts.most_common()),
        "samples": samples,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "explain which technical/risk/lifecycle gates context watch rows are waiting for",
        },
    }


def enrich_context_watchlist_freshness(
    payload: dict[str, Any],
    *,
    asof_date: pd.Timestamp | None = None,
) -> dict[str, Any]:
    """Attach the same freshness contract used by watchlist-builder skip logic."""

    out = dict(payload or {})
    active_watch_count = int(out.get("active_watch_count") or 0)
    confirmation_plan_summary = (
        out.get("technical_confirmation_plan_summary")
        if isinstance(out.get("technical_confirmation_plan_summary"), dict)
        else {}
    )
    active_confirmation_plan_count = int(
        out.get("active_confirmation_plan_count")
        if out.get("active_confirmation_plan_count") is not None
        else confirmation_plan_summary.get("plan_count") or 0
    )
    active_builder_version_count = int(out.get("active_builder_version_count") or 0)
    active_missing_confirmation_plan_count = max(0, active_watch_count - active_confirmation_plan_count)
    active_stale_builder_version_count = max(0, active_watch_count - active_builder_version_count)
    confirmation_plan_coverage_current = bool(active_watch_count > 0 and active_missing_confirmation_plan_count == 0)
    builder_version_current = bool(active_watch_count > 0 and active_stale_builder_version_count == 0)
    out["active_confirmation_plan_count"] = active_confirmation_plan_count
    out["active_missing_confirmation_plan_count"] = active_missing_confirmation_plan_count
    out["confirmation_plan_coverage_current"] = bool(confirmation_plan_coverage_current)
    out["active_builder_version_count"] = active_builder_version_count
    out["active_stale_builder_version_count"] = active_stale_builder_version_count
    out["builder_version_current"] = bool(builder_version_current)
    out["context_watchlist_builder_version"] = CONTEXT_OVERLAY_WATCHLIST_BUILDER_VERSION
    if active_watch_count <= 0:
        out.setdefault("current_for_reconcile", False)
        out.setdefault("context_signal_refresh_sync_state_available", False)
        out.setdefault("sync_asof_matches", False)
        out.setdefault("watchlist_fresh_for_signal_refresh", False)
        out.setdefault("context_watchlist_freshness_status", "stale_or_unverified_against_signal_refresh")
        return out

    effective_asof = pd.to_datetime(asof_date or out.get("asof_date"), utc=True, errors="coerce")
    effective_asof = None if pd.isna(effective_asof) else effective_asof.normalize()
    latest_load_ts = pd.to_datetime(out.get("latest_load_ts"), utc=True, errors="coerce")
    latest_load_ts = None if pd.isna(latest_load_ts) else latest_load_ts
    try:
        sync_row = load_sync_state(CONTEXT_OVERLAY_SIGNAL_REFRESH_SYNC_SOURCE) or {}
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_context_watchlist_signal_sync_state_failed",
            source=CONTEXT_OVERLAY_SIGNAL_REFRESH_SYNC_SOURCE,
            severity="warn",
            reason="Recommendation diagnostics could not inspect context signal-refresh sync state for watchlist freshness.",
            error=exc,
            metadata={"asof_date": None if effective_asof is None else effective_asof.isoformat()},
        )
        sync_row = {}

    sync_state = sync_row.get("state") if isinstance(sync_row.get("state"), dict) else {}
    sync_diagnostics = sync_state.get("diagnostics") if isinstance(sync_state.get("diagnostics"), dict) else {}
    sync_status = str(sync_row.get("status") or "").strip().lower() if sync_row else ""
    sync_asof = pd.to_datetime(sync_state.get("asof_date") or sync_diagnostics.get("asof_date"), utc=True, errors="coerce")
    sync_asof = None if pd.isna(sync_asof) else sync_asof.normalize()
    sync_last_success_at = pd.to_datetime(sync_row.get("last_success_at"), utc=True, errors="coerce") if sync_row else pd.NaT
    sync_last_success_at = None if pd.isna(sync_last_success_at) else sync_last_success_at
    sync_state_available = bool(sync_row) and sync_status == "ok"
    sync_asof_matches = bool(effective_asof is not None and sync_asof is not None and sync_asof == effective_asof)
    watchlist_fresh_for_signal_refresh = bool(
        latest_load_ts is not None
        and sync_last_success_at is not None
        and latest_load_ts >= sync_last_success_at
    )
    current = bool(
        active_watch_count > 0
        and sync_state_available
        and sync_asof_matches
        and watchlist_fresh_for_signal_refresh
        and confirmation_plan_coverage_current
        and builder_version_current
    )
    if current:
        freshness_status = "current"
    elif not confirmation_plan_coverage_current:
        freshness_status = "missing_technical_confirmation_plan"
    elif not builder_version_current:
        freshness_status = "stale_context_watchlist_builder_version"
    else:
        freshness_status = "stale_or_unverified_against_signal_refresh"
    out.update(
        {
            "current_for_reconcile": current,
            "context_watchlist_freshness_status": freshness_status,
            "context_signal_refresh_sync_state_available": bool(sync_state_available),
            "context_signal_refresh_status": sync_status or None,
            "context_signal_refresh_asof_date": None if sync_asof is None else sync_asof.isoformat(),
            "context_signal_refresh_last_success_at": None if sync_last_success_at is None else sync_last_success_at.isoformat(),
            "sync_asof_matches": bool(sync_asof_matches),
            "watchlist_fresh_for_signal_refresh": bool(watchlist_fresh_for_signal_refresh),
        }
    )
    if not current:
        if not confirmation_plan_coverage_current:
            out["operator_action"] = (
                "Context watchlist rows exist, but active rows are missing technical confirmation-plan metadata; "
                "run the bounded context watchlist reconciliation before treating watch pressure as current."
            )
        elif not builder_version_current:
            out["operator_action"] = (
                "Context watchlist rows exist, but active rows were created by an older context-watch selection algorithm; "
                "run the bounded context watchlist reconciliation before treating watch pressure as current."
            )
        else:
            out["operator_action"] = (
                "Context watchlist rows exist, but they are not proven current against context signal-refresh sync state; "
                "run the bounded context watchlist reconciliation before treating watch pressure as current."
            )
    return out


def load_context_watchlist_intake(*, asof_date: pd.Timestamp | None = None, limit: int = 500) -> dict[str, Any]:
    if not _table_exists(WATCHLIST_TABLE):
        payload = summarize_context_watchlist_intake(pd.DataFrame())
        payload["status"] = "watchlist_table_missing"
        payload["operator_action"] = "Run the watchlist builder/full advisory once so context-overlay watchlist intake can be diagnosed."
        return payload
    columns = _table_columns(WATCHLIST_TABLE)
    required = {"asof_date", "symbol"}
    if not required.issubset(columns):
        payload = summarize_context_watchlist_intake(pd.DataFrame())
        payload["status"] = "watchlist_schema_missing_required_columns"
        payload["missing_required_columns"] = sorted(required - columns)
        payload["operator_action"] = "Repair advisory_watchlist schema before diagnosing context-overlay watchlist intake."
        return payload
    context_filters: list[str] = []
    if "watch_source" in columns:
        context_filters.append("watch_source = 'context_overlay'")
    if "setup_id" in columns:
        context_filters.append("setup_id = 'CONTEXT_OVERLAY_WATCH'")
    if "context_policy_effect" in columns:
        context_filters.append("context_policy_effect IS NOT NULL")
    if not context_filters:
        payload = summarize_context_watchlist_intake(pd.DataFrame())
        payload["status"] = "watchlist_schema_missing_context_columns"
        payload["operator_action"] = "Run the watchlist schema migration/full advisory so context-overlay watchlist rows can be identified."
        return payload
    context_where = "(" + " OR ".join(context_filters) + ")"
    effective_asof = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date is not None else None
    effective_asof = None if effective_asof is None or pd.isna(effective_asof) else effective_asof.normalize()
    if effective_asof is None:
        latest = sql_to_df(f"SELECT MAX(asof_date) AS asof_date FROM {WATCHLIST_TABLE} WHERE {context_where}")
        if latest.empty:
            return summarize_context_watchlist_intake(pd.DataFrame())
        parsed = pd.to_datetime(latest["asof_date"].iloc[0], utc=True, errors="coerce")
        effective_asof = None if pd.isna(parsed) else parsed.normalize()
    if effective_asof is None:
        return summarize_context_watchlist_intake(pd.DataFrame())
    select_cols = [
        "asof_date",
        "symbol",
        _optional_select("candidate_state", columns),
        _optional_select("current_state", columns),
        _optional_select("watch_status", columns),
        _optional_select("watch_enabled", columns, cast="BOOLEAN"),
        _optional_select("context_source", columns),
        _optional_select("context_policy_effect", columns),
        _optional_select("context_reliability_classification", columns),
        _optional_select("context_class_reliability_classification", columns),
        _optional_select("context_authority_scope", columns),
        _optional_select("watch_reason_detail", columns),
        _optional_select("watch_reasons_json", columns),
        _optional_select("load_ts", columns, cast="TIMESTAMPTZ"),
    ]
    watch_enabled_expr = "COALESCE(watch_enabled, false) IS TRUE" if "watch_enabled" in columns else "FALSE"
    current_state_expr = "UPPER(TRIM(COALESCE(current_state, '')))" if "current_state" in columns else "''"
    candidate_state_expr = "UPPER(TRIM(COALESCE(candidate_state, '')))" if "candidate_state" in columns else "''"
    watch_status_expr = "LOWER(TRIM(COALESCE(watch_status, '')))" if "watch_status" in columns else "''"
    policy_effect_expr = "TRIM(COALESCE(context_policy_effect, ''))" if "context_policy_effect" in columns else "''"
    active_expr = f"({watch_enabled_expr} AND {current_state_expr} NOT IN ('REJECT', 'ABSTAIN'))"
    breakout_expr = f"({active_expr} AND {candidate_state_expr} = 'WATCH_BREAKOUT')"
    suppressed_expr = (
        f"({current_state_expr} IN ('REJECT', 'ABSTAIN') "
        f"OR {watch_status_expr} IN ('rejected', 'abstained') "
        f"OR {policy_effect_expr} LIKE 'suppress_%%')"
    )
    confirmation_plan_expr = (
        "COALESCE(watch_reasons_json, '') LIKE '%%\"technical_confirmation_plan\"%%'"
        if "watch_reasons_json" in columns
        else "FALSE"
    )
    builder_version_expr = (
        f"COALESCE(watch_reasons_json, '') LIKE '%%\"context_watchlist_builder_version\": \"{CONTEXT_OVERLAY_WATCHLIST_BUILDER_VERSION}\"%%'"
        if "watch_reasons_json" in columns
        else "FALSE"
    )
    latest_load_select = "MAX(load_ts) AS latest_load_ts" if "load_ts" in columns else "NULL::TIMESTAMPTZ AS latest_load_ts"
    aggregate_df = pd.DataFrame()
    try:
        aggregate_df = sql_to_df(
            f"""
            SELECT
                COUNT(*) AS total_row_count,
                SUM(CASE WHEN {active_expr} THEN 1 ELSE 0 END) AS total_active_watch_count,
                SUM(CASE WHEN {active_expr} AND {confirmation_plan_expr} THEN 1 ELSE 0 END) AS total_active_confirmation_plan_count,
                SUM(CASE WHEN {active_expr} AND {builder_version_expr} THEN 1 ELSE 0 END) AS total_active_builder_version_count,
                SUM(CASE WHEN {breakout_expr} THEN 1 ELSE 0 END) AS total_breakout_watch_count,
                SUM(CASE WHEN {suppressed_expr} THEN 1 ELSE 0 END) AS total_suppressed_count,
                SUM(CASE WHEN {policy_effect_expr} = 'suppress_context_identity_unresolved_no_trade_authority' THEN 1 ELSE 0 END) AS total_identity_suppressed_count,
                SUM(CASE WHEN {policy_effect_expr} = 'suppress_context_class_watch_only_no_sell_authority' THEN 1 ELSE 0 END) AS total_class_suppressed_count,
                SUM(CASE WHEN {policy_effect_expr} = 'suppress_context_watch_only_no_sell_authority' THEN 1 ELSE 0 END) AS total_negative_context_suppressed_count,
                {latest_load_select}
            FROM {WATCHLIST_TABLE}
            WHERE asof_date = %s
              AND {context_where}
            """,
            params=(effective_asof,),
        )
        df = sql_to_df(
            f"""
            SELECT {', '.join(select_cols)}
            FROM {WATCHLIST_TABLE}
            WHERE asof_date = %s
              AND {context_where}
            ORDER BY watch_enabled DESC NULLS LAST, candidate_state, symbol
            LIMIT %s
            """,
            params=(effective_asof, max(1, int(limit))),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_context_watchlist_intake_failed",
            source=WATCHLIST_TABLE,
            severity="warn",
            reason="Recommendation diagnostics could not load persisted context-overlay watchlist rows.",
            error=exc,
            metadata={"asof_date": str(effective_asof), "limit": int(limit)},
        )
        payload = summarize_context_watchlist_intake(pd.DataFrame())
        payload["status"] = "context_watchlist_intake_load_failed"
        payload["empty_reason"] = f"{exc.__class__.__name__}: {str(exc)[:200]}"
        return payload
    latest_context_rows = pd.DataFrame()
    if df.empty:
        try:
            latest_context_rows = sql_to_df(
                f"""
                SELECT asof_date, COUNT(*) AS row_count
                FROM {WATCHLIST_TABLE}
                WHERE {context_where}
                GROUP BY asof_date
                ORDER BY asof_date DESC
                LIMIT 1
                """
            )
        except Exception as exc:
            record_local_fallback_event(
                module="advisory.recommendation_diagnostics",
                fallback_type="recommendation_diagnostics_context_watchlist_latest_lookup_failed",
                source=WATCHLIST_TABLE,
                severity="warn",
                reason="Recommendation diagnostics could not load latest context-overlay watchlist date after an empty as-of lookup.",
                error=exc,
                metadata={"asof_date": str(effective_asof), "limit": int(limit)},
            )
    payload = summarize_context_watchlist_intake(df)
    payload["asof_date"] = effective_asof.isoformat()
    if not aggregate_df.empty and "total_row_count" in aggregate_df.columns:
        aggregate_row = aggregate_df.iloc[0].to_dict()
        def _aggregate_int(key: str) -> int:
            value = pd.to_numeric(aggregate_row.get(key), errors="coerce")
            return 0 if pd.isna(value) else int(value)

        latest_load_ts = pd.to_datetime(aggregate_row.get("latest_load_ts"), utc=True, errors="coerce")
        latest_load_ts = None if pd.isna(latest_load_ts) else latest_load_ts
        payload.update(
            {
                "row_count": _aggregate_int("total_row_count"),
                "active_watch_count": _aggregate_int("total_active_watch_count"),
                "active_confirmation_plan_count": _aggregate_int("total_active_confirmation_plan_count"),
                "active_builder_version_count": _aggregate_int("total_active_builder_version_count"),
                "breakout_watch_count": _aggregate_int("total_breakout_watch_count"),
                "suppressed_count": _aggregate_int("total_suppressed_count"),
                "identity_suppressed_count": _aggregate_int("total_identity_suppressed_count"),
                "class_suppressed_count": _aggregate_int("total_class_suppressed_count"),
                "negative_context_suppressed_count": _aggregate_int("total_negative_context_suppressed_count"),
                "latest_load_ts": None if latest_load_ts is None else latest_load_ts.isoformat(),
                "sample_row_count": int(len(df)),
                "sample_limit": int(limit),
                "counts_scope": "full_asof_snapshot",
            }
        )
    else:
        payload["sample_row_count"] = int(len(df))
        payload["sample_limit"] = int(limit)
        payload["counts_scope"] = "limited_sample"
    payload = enrich_context_watchlist_freshness(payload, asof_date=effective_asof)
    if df.empty and not latest_context_rows.empty:
        latest_asof = pd.to_datetime(latest_context_rows["asof_date"].iloc[0], utc=True, errors="coerce")
        latest_count_raw = pd.to_numeric(latest_context_rows["row_count"].iloc[0], errors="coerce")
        latest_count = 0 if pd.isna(latest_count_raw) else int(latest_count_raw)
        if pd.notna(latest_asof) and latest_asof.normalize() != effective_asof.normalize():
            payload["status"] = "context_watchlist_rows_exist_for_different_asof"
            payload["latest_context_watchlist_asof_date"] = latest_asof.normalize().isoformat()
            payload["latest_context_watchlist_row_count"] = latest_count
            payload["date_mismatch_days"] = int((latest_asof.normalize() - effective_asof.normalize()).days)
            payload["operator_action"] = (
                "No context-overlay watchlist rows exist for the diagnostic as-of date, but rows exist for another date; "
                "run the bounded context watchlist reconciliation for the diagnostic date or rerun advisory on the latest trading date."
            )
    return payload


def load_context_watch_technical_precheck(
    *,
    asof_date: pd.Timestamp | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Read-only technical coverage check for active context-watch symbols."""

    effective_asof = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date is not None else None
    effective_asof = None if effective_asof is None or pd.isna(effective_asof) else effective_asof.normalize()
    if effective_asof is None:
        return {
            "status": "missing_asof_date",
            "active_context_watch_symbol_count": 0,
            "technical_symbol_coverage_count": 0,
            "missing_technical_symbol_count": 0,
            "operator_action": "Provide an as-of date before checking context-watch technical coverage.",
        }
    if not _table_exists(WATCHLIST_TABLE):
        return {
            "status": "watchlist_table_missing",
            "asof_date": effective_asof.isoformat(),
            "active_context_watch_symbol_count": 0,
            "technical_symbol_coverage_count": 0,
            "missing_technical_symbol_count": 0,
            "operator_action": "Run watchlist reconciliation before checking technical coverage for context-watch rows.",
        }
    if not _table_exists(TECHNICAL_DAILY_TABLE):
        return {
            "status": "technical_table_missing",
            "asof_date": effective_asof.isoformat(),
            "active_context_watch_symbol_count": 0,
            "technical_symbol_coverage_count": 0,
            "missing_technical_symbol_count": 0,
            "operator_action": "Build advisory technical features before expecting context-watch symbols to become technical candidates.",
        }
    try:
        watch_symbols_df = sql_to_df(
            f"""
            SELECT DISTINCT UPPER(TRIM(symbol)) AS symbol
            FROM {WATCHLIST_TABLE}
            WHERE asof_date = %s
              AND setup_id = 'CONTEXT_OVERLAY_WATCH'
              AND COALESCE(watch_enabled, false) IS TRUE
              AND UPPER(TRIM(COALESCE(current_state, ''))) NOT IN ('REJECT', 'ABSTAIN')
              AND symbol IS NOT NULL
            ORDER BY symbol
            """,
            params=(effective_asof,),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_context_watch_technical_symbols_failed",
            source=WATCHLIST_TABLE,
            severity="warn",
            reason="Recommendation diagnostics could not load active context-watch symbols for technical precheck.",
            error=exc,
            metadata={"asof_date": effective_asof.isoformat()},
        )
        return {
            "status": "context_watch_symbols_load_failed",
            "asof_date": effective_asof.isoformat(),
            "active_context_watch_symbol_count": 0,
            "technical_symbol_coverage_count": 0,
            "missing_technical_symbol_count": 0,
            "error_type": exc.__class__.__name__,
        }
    symbols = (
        watch_symbols_df["symbol"].astype("string").dropna().str.strip().str.upper().drop_duplicates().tolist()
        if not watch_symbols_df.empty and "symbol" in watch_symbols_df.columns
        else []
    )
    if not symbols:
        return {
            "status": "no_active_context_watch_symbols",
            "asof_date": effective_asof.isoformat(),
            "active_context_watch_symbol_count": 0,
            "technical_symbol_coverage_count": 0,
            "missing_technical_symbol_count": 0,
            "operator_action": "No active context-watch symbols exist for this as-of date.",
        }
    latest_trading_day = load_latest_trading_day_on_or_before(effective_asof)
    identity_availability = _load_dhan_identity_availability(symbols)
    identity_resolved_symbols = set(identity_availability.get("resolved_symbols") or [])
    identity_status = str(identity_availability.get("status") or "").strip()
    identity_checked = identity_status in {"identity_available", "identity_partial"}
    refresh_symbols = [symbol for symbol in symbols if not identity_checked or symbol in identity_resolved_symbols]
    dhan_daily_availability = _load_dhan_daily_availability(refresh_symbols or symbols, effective_asof)
    latest_dhan_daily_date = pd.to_datetime(
        dhan_daily_availability.get("latest_available_date"),
        utc=True,
        errors="coerce",
    )
    if pd.isna(latest_dhan_daily_date):
        latest_dhan_daily_date = None
    else:
        latest_dhan_daily_date = latest_dhan_daily_date.normalize()
    technical_command_date = latest_trading_day or effective_asof
    if latest_dhan_daily_date is not None and latest_dhan_daily_date < technical_command_date:
        technical_command_date = latest_dhan_daily_date
    refresh_status = _load_latest_technical_refresh_status(symbols, technical_command_date)
    newer_refresh_status = (
        _load_latest_technical_refresh_status(symbols, effective_asof)
        if effective_asof.normalize() > technical_command_date.normalize()
        else {"status": "not_applicable", "issue_symbol_count": 0, "issue_symbols": []}
    )
    technical_refresh_command = " && ".join(
        [
            "python -m data.dhanlive.scrip_master",
            "python -m data.company_master",
        ]
    ) if identity_checked and not refresh_symbols else (
        "python -m advisory.technical_features "
        "--targeted-context-refresh "
        f"--from-date {technical_command_date.date().isoformat()} "
        f"--to-date {technical_command_date.date().isoformat()} "
        "--symbols "
        + " ".join(refresh_symbols)
    )
    try:
        tech_df = sql_to_df(
            f"""
            SELECT DISTINCT ON (UPPER(TRIM(symbol)))
                UPPER(TRIM(symbol)) AS symbol,
                asof_date,
                adj_close,
                avg_traded_value_20d,
                rs_vs_benchmark,
                rs_vs_sector,
                pivot_distance_20d_pct,
                dist_52w_high,
                pass_liquidity_20d,
                pass_trend_alignment,
                pass_breakout_extension,
                pass_gap_behavior,
                load_ts
            FROM {TECHNICAL_DAILY_TABLE}
            WHERE asof_date <= %s
              AND UPPER(TRIM(symbol)) = ANY(%s)
              AND UPPER(TRIM(COALESCE(series, 'daily'))) IN ('DAILY', 'EQ')
            ORDER BY UPPER(TRIM(symbol)), asof_date DESC, load_ts DESC NULLS LAST
            LIMIT %s
            """,
            params=(effective_asof, symbols, max(int(limit), int(len(symbols)))),
        )
        latest_technical_asof = None
        if not tech_df.empty and "asof_date" in tech_df.columns:
            latest_series = pd.to_datetime(tech_df["asof_date"], utc=True, errors="coerce").dropna()
            latest_technical_asof = None if latest_series.empty else latest_series.max().normalize()
        if tech_df.empty or latest_technical_asof is None:
            input_blockers = _summarize_technical_input_blockers(
                refresh_status=refresh_status,
                missing_symbols=symbols,
            )
            return {
                "status": "no_technical_rows_for_context_watch_symbols",
                "asof_date": effective_asof.isoformat(),
                "latest_trading_day": None if latest_trading_day is None else latest_trading_day.isoformat(),
                "latest_dhan_daily_date": None if latest_dhan_daily_date is None else latest_dhan_daily_date.isoformat(),
                "technical_command_date": technical_command_date.isoformat(),
                "dhan_daily_availability": dhan_daily_availability,
                "active_context_watch_symbol_count": int(len(symbols)),
                "technical_symbol_coverage_count": 0,
                "missing_technical_symbol_count": int(len(symbols)),
                "dhan_identity_status": identity_availability.get("status"),
                "dhan_identity_coverage_count": int(identity_availability.get("dhan_identity_coverage_count") or 0),
                "missing_dhan_identity_count": int(identity_availability.get("missing_dhan_identity_count") or 0),
                "missing_dhan_identity_sample": identity_availability.get("missing_dhan_identity_sample") or [],
                "technical_refresh_symbol_count": int(len(refresh_symbols)),
                "latest_refresh_status": refresh_status,
                "newer_refresh_attempt_status": newer_refresh_status,
                "technical_input_blockers": input_blockers,
                "recommended_command": technical_refresh_command,
                "operator_action": "Active context-watch symbols have no point-in-time technical feature rows; refresh OHLCV and technical features.",
                "policy_boundary": {
                    "report_mode": "read_only_diagnostic",
                    "portfolio_authority": "none",
                    "broker_execution_allowed": False,
                },
            }
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_context_watch_technical_precheck_failed",
            source=TECHNICAL_DAILY_TABLE,
            severity="warn",
            reason="Recommendation diagnostics could not load point-in-time technical rows for context-watch symbols.",
            error=exc,
            metadata={"asof_date": effective_asof.isoformat(), "symbol_count": len(symbols)},
        )
        return {
            "status": "technical_precheck_load_failed",
            "asof_date": effective_asof.isoformat(),
            "active_context_watch_symbol_count": int(len(symbols)),
            "technical_symbol_coverage_count": 0,
            "missing_technical_symbol_count": int(len(symbols)),
            "error_type": exc.__class__.__name__,
        }

    frame = tech_df.copy()
    covered_symbols = (
        set(frame["symbol"].astype("string").dropna().str.strip().str.upper().tolist())
        if not frame.empty and "symbol" in frame.columns
        else set()
    )
    missing_symbols = sorted(set(symbols) - covered_symbols)
    if frame.empty:
        frame = pd.DataFrame(columns=["symbol"])
    for column in ["pass_liquidity_20d", "pass_trend_alignment", "pass_breakout_extension", "pass_gap_behavior"]:
        if column not in frame.columns:
            frame[column] = False
    for column in ["pivot_distance_20d_pct", "rs_vs_benchmark", "rs_vs_sector", "avg_traded_value_20d", "dist_52w_high", "adj_close"]:
        if column not in frame.columns:
            frame[column] = pd.NA
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    liquidity_mask = frame["pass_liquidity_20d"].map(_boolish)
    trend_mask = frame["pass_trend_alignment"].map(_boolish)
    breakout_extension_mask = frame["pass_breakout_extension"].map(_boolish)
    gap_mask = frame["pass_gap_behavior"].map(_boolish)
    near_pivot_mask = frame["pivot_distance_20d_pct"].abs().le(5.0).fillna(False)
    rs_positive_mask = frame["rs_vs_benchmark"].gt(0).fillna(False)
    constructive_mask = liquidity_mask & trend_mask & near_pivot_mask & breakout_extension_mask & gap_mask
    breakout_like_mask = constructive_mask & rs_positive_mask
    frame["asof_date"] = pd.to_datetime(frame.get("asof_date"), utc=True, errors="coerce").dt.normalize()
    stale_days = None
    stale_symbol_count = 0
    stale_symbols: list[str] = []
    stale_symbol_sample: list[str] = []
    if latest_trading_day is not None and latest_technical_asof is not None:
        stale_reference_day = technical_command_date
        stale_days = int((stale_reference_day - latest_technical_asof).days)
        stale_mask = frame["asof_date"].lt(stale_reference_day).fillna(True)
        stale_symbol_count = int(stale_mask.sum())
        if "symbol" in frame.columns:
            stale_symbols = (
                frame.loc[stale_mask, "symbol"]
                .astype("string")
                .dropna()
                .str.strip()
                .str.upper()
                .drop_duplicates()
                .tolist()
            )
            stale_symbol_sample = stale_symbols[:10]
    technical_features_stale = bool(stale_symbol_count > 0)
    missing_dhan_identity_symbols = identity_availability.get("missing_symbols") or []
    missing_dhan_identity_count = int(identity_availability.get("missing_dhan_identity_count") or 0)
    missing_technical_with_identity = sorted(set(missing_symbols) - set(missing_dhan_identity_symbols))
    technical_input_blockers = _summarize_technical_input_blockers(
        refresh_status=refresh_status,
        missing_symbols=missing_symbols,
    )

    sample_frame = frame.assign(
        _constructive=constructive_mask,
        _near_pivot=near_pivot_mask,
        _pivot_abs=frame["pivot_distance_20d_pct"].abs(),
    ).sort_values(
        by=["_constructive", "_near_pivot", "rs_vs_benchmark", "_pivot_abs", "symbol"],
        ascending=[False, False, False, True, True],
        kind="mergesort",
    )
    samples: list[dict[str, Any]] = []
    for item in sample_frame.head(10).to_dict(orient="records"):
        samples.append(
            {
                "symbol": _text(item.get("symbol")),
                "adj_close": _num(item.get("adj_close")),
                "pivot_distance_20d_pct": _num(item.get("pivot_distance_20d_pct")),
                "rs_vs_benchmark": _num(item.get("rs_vs_benchmark")),
                "rs_vs_sector": _num(item.get("rs_vs_sector")),
                "avg_traded_value_20d": _num(item.get("avg_traded_value_20d")),
                "pass_liquidity_20d": _boolish(item.get("pass_liquidity_20d")),
                "pass_trend_alignment": _boolish(item.get("pass_trend_alignment")),
                "near_pivot_5pct": bool(item.get("_near_pivot")),
                "constructive_precheck": bool(item.get("_constructive")),
            }
        )

    status = "technical_precheck_current"
    operator_action = "Context-watch symbols have point-in-time technical feature coverage; use full advisory/rule-engine generation to create authoritative candidates."
    if technical_features_stale:
        status = "technical_precheck_stale"
        operator_action = "Context-watch symbols have technical feature rows, but they are older than the latest trading day; refresh technical features or rerun advisory."
    elif (
        latest_trading_day is not None
        and latest_dhan_daily_date is not None
        and latest_dhan_daily_date < latest_trading_day
    ):
        status = "technical_precheck_current_to_dhan_lagging_calendar"
        operator_action = (
            "Context-watch technical rows are current to the latest available Dhan daily OHLCV date, but Dhan daily data lags the exchange trading calendar; "
            "do not treat missing BUYs as regime suppression until Dhan daily data advances."
        )
    elif int(len(covered_symbols)) <= 0:
        status = "technical_precheck_missing"
        operator_action = "Active context-watch symbols lack technical feature rows; refresh OHLCV and technical features."
    elif missing_dhan_identity_count > 0 and len(missing_technical_with_identity) <= 0 and missing_symbols:
        status = "technical_precheck_blocked_by_dhan_identity"
        operator_action = "Some context-watch symbols lack Dhan identity mapping; refresh/repair Dhan and company masters before expecting technical features for those symbols."
    elif int(constructive_mask.sum()) > 0:
        status = "technical_precheck_has_constructive_symbols"
        operator_action = "Some context-watch symbols have constructive technical precheck evidence; rerun full advisory so rule-engine candidates can reconcile them."
    elif missing_dhan_identity_count > 0:
        operator_action = (
            "Context-watch symbols have partial technical coverage and some missing Dhan identities; refresh technical features for resolvable symbols "
            "and repair Dhan/company-master mappings for unresolved symbols before changing regime or thresholds."
        )
    if int(refresh_status.get("issue_symbol_count") or 0) > 0:
        operator_action = (
            f"{operator_action} Latest technical refresh status has issues for "
            f"{int(refresh_status.get('issue_symbol_count') or 0)} active context-watch symbols; inspect "
            f"{TECHNICAL_REFRESH_STATUS_TABLE} before rerunning full advisory."
        )
    if int(technical_input_blockers.get("hard_blocker_symbol_count") or 0) > 0:
        operator_action = (
            f"{operator_action} Hard OHLCV input blockers exist for "
            f"{int(technical_input_blockers.get('hard_blocker_symbol_count') or 0)} symbols; suppress or repair those context-watch rows until usable Dhan daily history exists."
        )
    if int(newer_refresh_status.get("issue_symbol_count") or 0) > int(refresh_status.get("issue_symbol_count") or 0):
        operator_action = (
            f"{operator_action} Newer refresh attempts after the Dhan-backed command date still show "
            f"{int(newer_refresh_status.get('issue_symbol_count') or 0)} issue symbols; treat those as feed/calendar-lag evidence, "
            "not as proof that the Dhan-backed repair date failed."
        )
    if latest_trading_day is not None and latest_dhan_daily_date is not None and latest_dhan_daily_date < latest_trading_day:
        operator_action = (
            f"{operator_action} Dhan daily OHLCV currently reaches "
            f"{latest_dhan_daily_date.date().isoformat()} while the latest trading day is "
            f"{latest_trading_day.date().isoformat()}; use the Dhan-backed technical command date until the feed advances."
        )

    latest_refresh_issue_symbols = refresh_status.get("issue_symbols") or []
    newer_refresh_issue_symbols = newer_refresh_status.get("issue_symbols") or []
    actionable_refresh_symbols = sorted(
        {
            str(symbol or "").strip().upper()
            for symbol in (
                list(missing_technical_with_identity)
                + list(stale_symbols)
                + list(latest_refresh_issue_symbols)
                + list(newer_refresh_issue_symbols)
            )
            if str(symbol or "").strip()
        }
    )
    narrow_refresh_command = technical_context_refresh_command(
        technical_command_date,
        actionable_refresh_symbols,
    )

    return {
        "status": status,
        "asof_date": effective_asof.isoformat(),
        "latest_trading_day": None if latest_trading_day is None else latest_trading_day.isoformat(),
        "latest_dhan_daily_date": None if latest_dhan_daily_date is None else latest_dhan_daily_date.isoformat(),
        "technical_command_date": technical_command_date.isoformat(),
        "dhan_daily_availability": dhan_daily_availability,
        "latest_technical_asof_date": None if latest_technical_asof is None else latest_technical_asof.isoformat(),
        "technical_stale_days": stale_days,
        "technical_features_stale": bool(technical_features_stale),
        "stale_technical_symbol_count": int(stale_symbol_count),
        "stale_technical_symbols": stale_symbols,
        "stale_technical_symbol_sample": stale_symbol_sample,
        "active_context_watch_symbol_count": int(len(symbols)),
        "technical_symbol_coverage_count": int(len(covered_symbols)),
        "missing_technical_symbol_count": int(len(missing_symbols)),
        "missing_symbol_sample": missing_symbols[:10],
        "dhan_identity_status": identity_availability.get("status"),
        "dhan_identity_coverage_count": int(identity_availability.get("dhan_identity_coverage_count") or 0),
        "missing_dhan_identity_count": missing_dhan_identity_count,
        "missing_dhan_identity_sample": identity_availability.get("missing_dhan_identity_sample") or [],
        "missing_technical_with_identity_count": int(len(missing_technical_with_identity)),
        "missing_technical_with_identity_sample": missing_technical_with_identity[:10],
        "technical_refresh_symbol_count": int(len(refresh_symbols)),
        "latest_refresh_status": refresh_status,
        "newer_refresh_attempt_status": newer_refresh_status,
        "technical_input_blockers": technical_input_blockers,
        "latest_refresh_issue_symbol_count": int(refresh_status.get("issue_symbol_count") or 0),
        "latest_refresh_issue_symbols": latest_refresh_issue_symbols,
        "latest_refresh_issue_symbol_sample": refresh_status.get("issue_symbol_sample") or [],
        "newer_refresh_attempt_issue_symbol_count": int(newer_refresh_status.get("issue_symbol_count") or 0),
        "newer_refresh_attempt_issue_symbols": newer_refresh_issue_symbols,
        "newer_refresh_attempt_issue_symbol_sample": newer_refresh_status.get("issue_symbol_sample") or [],
        "recommended_command": technical_refresh_command,
        "targeted_repair_symbol_count": int(len(actionable_refresh_symbols)),
        "targeted_repair_symbols": actionable_refresh_symbols,
        "targeted_repair_symbol_sample": actionable_refresh_symbols[:10],
        "targeted_repair_command": narrow_refresh_command or None,
        "liquidity_pass_count": int(liquidity_mask.sum()),
        "trend_alignment_pass_count": int(trend_mask.sum()),
        "near_pivot_5pct_count": int(near_pivot_mask.sum()),
        "rs_positive_count": int(rs_positive_mask.sum()),
        "constructive_precheck_count": int(constructive_mask.sum()),
        "breakout_like_precheck_count": int(breakout_like_mask.sum()),
        "samples": samples,
        "operator_action": operator_action,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "show whether context-watch symbols have point-in-time technical feature coverage before full advisory candidate generation",
        },
    }


def load_context_overlay_source_diagnostics(*, asof_date: pd.Timestamp | None = None) -> dict[str, Any]:
    try:
        from advisory.signal_refresh import inspect_context_overlay_signal_inputs

        return summarize_context_overlay_source_availability(
            inspect_context_overlay_signal_inputs(asof_date=asof_date)
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_context_source_diagnostics_failed",
            source="context_overlay_signal_inputs",
            severity="warn",
            reason="Recommendation diagnostics could not inspect context-overlay source-table availability.",
            error=exc,
            metadata={"asof_date": None if asof_date is None else str(asof_date)},
        )
        return summarize_context_overlay_source_availability(
            {
                "source_status": "diagnostic_failed",
                "source_rows_total": 0,
                "active_authorized_rows_total": 0,
                "empty_reason": f"{exc.__class__.__name__}: {str(exc)[:200]}",
            }
        )


def summarize_context_overlay_refresh_preview(preview: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(preview, dict):
        return {
            "status": "not_loaded",
            "operator_action": "Context-overlay refresh preview was not loaded for this in-memory diagnostic call.",
            "policy_boundary": {
                "report_mode": "read_only_diagnostic",
                "portfolio_authority": "none",
                "broker_execution_allowed": False,
                "decision_use": "preview whether context-overlay evidence can create review-only Action Queue rows before a long advisory rerun",
            },
        }
    if (
        str(preview.get("status") or "").startswith("context_overlay_")
        and isinstance(preview.get("policy_boundary"), dict)
        and "signal_rows" in preview
    ):
        return preview
    target_rows = int(preview.get("target_rows") or 0)
    signal_rows = int(preview.get("signal_rows") or 0)
    affected_symbols = [
        str(value).strip().upper()
        for value in (preview.get("affected_symbols") or [])
        if str(value).strip()
    ]
    asof_value = preview.get("diagnostics", {}).get("asof_date") if isinstance(preview.get("diagnostics"), dict) else None
    asof_value = asof_value or preview.get("asof_date") or preview.get("requested_asof_date")
    asof_ts = pd.to_datetime(asof_value, utc=True, errors="coerce") if asof_value is not None else None
    asof_text = None if asof_ts is None or pd.isna(asof_ts) else asof_ts.date().isoformat()
    command = "python -m advisory.signal_refresh --from-context-overlays --limit 50 --format text"
    if asof_text:
        command = f"python -m advisory.signal_refresh --from-context-overlays --asof-date {asof_text} --limit 50 --format text"
    if signal_rows > 0:
        status = "context_overlay_refresh_would_emit_review_only_signals"
        operator_action = (
            "A read-only preview shows context overlays can emit review-only Action Queue signals now; run the bounded "
            "context-overlay signal refresh before the long full-advisory reconciliation."
        )
    elif target_rows > 0:
        status = "context_overlay_targets_without_signal_rows"
        operator_action = "Context-overlay targets exist but did not become signal rows; inspect conversion_diagnostics before treating context evidence as absent."
    else:
        status = "no_context_overlay_refresh_targets"
        operator_action = "Context-overlay refresh preview found no matching watchlist/portfolio targets for this date."
    return {
        "status": status,
        "requested_asof_date": asof_text,
        "target_rows": target_rows,
        "signal_rows": signal_rows,
        "affected_symbol_count": int(len(affected_symbols)),
        "affected_symbols": affected_symbols[:50],
        "raw_theme_target_rows": int(preview.get("raw_theme_target_rows") or 0),
        "raw_macro_context_target_rows": int(preview.get("raw_macro_context_target_rows") or 0),
        "raw_direct_context_target_rows": int(preview.get("raw_direct_context_target_rows") or 0),
        "theme_target_rows": int(preview.get("theme_target_rows") or 0),
        "macro_context_target_rows": int(preview.get("macro_context_target_rows") or 0),
        "direct_context_target_rows": int(preview.get("direct_context_target_rows") or 0),
        "positive_watch_target_rows": int(preview.get("positive_watch_target_rows") or 0),
        "reliability_suppressed_target_rows": int(preview.get("reliability_suppressed_target_rows") or 0),
        "empty_reason": preview.get("empty_reason"),
        "conversion_diagnostics": preview.get("conversion_diagnostics") if isinstance(preview.get("conversion_diagnostics"), dict) else {},
        "recommended_command": command if signal_rows > 0 else None,
        "operator_action": operator_action,
        "authority_contract": {
            "authority_scope": str(preview.get("authority_scope") or "review_input_only"),
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "full_advisory_required": True,
        },
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "authority_scope": str(preview.get("authority_scope") or "review_input_only"),
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "full_advisory_required": True,
            "action_refresh_authority": "review_only_action_queue_visibility",
            "full_advisory_still_authoritative": True,
            "decision_use": "preview whether context-overlay evidence can create review-only Action Queue rows before a long advisory rerun",
        },
    }


def load_context_overlay_refresh_preview(*, asof_date: pd.Timestamp | None = None, limit: int = 50) -> dict[str, Any]:
    try:
        from advisory.signal_refresh import preview_context_overlay_signal_refresh

        return summarize_context_overlay_refresh_preview(
            preview_context_overlay_signal_refresh(asof_date=asof_date, limit=limit)
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_context_refresh_preview_failed",
            source="context_overlay_signal_refresh_preview",
            severity="warn",
            reason="Recommendation diagnostics could not build the read-only context-overlay signal-refresh preview.",
            error=exc,
            metadata={"asof_date": None if asof_date is None else str(asof_date), "limit": int(limit)},
        )
        return summarize_context_overlay_refresh_preview(
            {
                "target_rows": 0,
                "signal_rows": 0,
                "empty_reason": f"{exc.__class__.__name__}: {str(exc)[:200]}",
            }
        )


def summarize_context_overlay_signal_refresh_state(
    sync_state: dict[str, Any] | None,
    *,
    context_overlay_refresh_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preview = context_overlay_refresh_preview if isinstance(context_overlay_refresh_preview, dict) else {}
    expected_asof = pd.to_datetime(
        preview.get("requested_asof_date")
        or preview.get("asof_date")
        or ((preview.get("diagnostics") or {}).get("asof_date") if isinstance(preview.get("diagnostics"), dict) else None),
        utc=True,
        errors="coerce",
    )
    expected_asof = None if pd.isna(expected_asof) else expected_asof.normalize()
    expected_asof_iso = None if expected_asof is None else expected_asof.isoformat()
    preview_signal_rows = int(preview.get("signal_rows") or 0)
    preview_target_rows = int(preview.get("target_rows") or 0)
    if isinstance(sync_state, dict) and "state_signal_rows" in sync_state and "state" not in sync_state:
        state_asof = pd.to_datetime(sync_state.get("state_asof_date"), utc=True, errors="coerce")
        state_asof = None if pd.isna(state_asof) else state_asof.normalize()
        state_signal_rows = int(sync_state.get("state_signal_rows") or 0)
        state_target_rows = int(sync_state.get("state_target_rows") or 0)
        status_text = str(sync_state.get("state_status") or ("ok" if sync_state.get("status") == "current" else "")).strip().lower()
        asof_matches = expected_asof is None or (state_asof is not None and state_asof == expected_asof)
        row_counts_cover_preview = state_signal_rows >= preview_signal_rows and state_target_rows >= preview_target_rows
        authority_safe = _boolish(sync_state.get("authority_safe"))
        current = bool(status_text == "ok" and asof_matches and row_counts_cover_preview and authority_safe)
        return {
            **sync_state,
            "status": "current" if current else "stale_or_mismatched",
            "source_name": CONTEXT_OVERLAY_SIGNAL_REFRESH_SYNC_SOURCE,
            "expected_asof_date": expected_asof_iso,
            "state_asof_date": None if state_asof is None else state_asof.isoformat(),
            "asof_matches": bool(asof_matches),
            "state_status": status_text,
            "state_signal_rows": state_signal_rows,
            "state_target_rows": state_target_rows,
            "preview_signal_rows": preview_signal_rows,
            "preview_target_rows": preview_target_rows,
            "row_counts_cover_preview": bool(row_counts_cover_preview),
            "authority_safe": bool(authority_safe),
            "current_for_preview": bool(current),
            "signal_refresh_recommended": bool(preview_signal_rows > 0 and not current),
            "operator_action": (
                "Bounded context-overlay signal refresh is already current for the preview; do not rerun it before full advisory."
                if current
                else "Run bounded context-overlay signal refresh before full advisory if preview rows exist."
            ),
        }
    if not isinstance(sync_state, dict) or not sync_state:
        return {
            "status": "missing_sync_state",
            "source_name": CONTEXT_OVERLAY_SIGNAL_REFRESH_SYNC_SOURCE,
            "expected_asof_date": expected_asof_iso,
            "state_asof_date": None,
            "current_for_preview": False,
            "signal_refresh_recommended": preview_signal_rows > 0,
            "preview_signal_rows": preview_signal_rows,
            "preview_target_rows": preview_target_rows,
            "operator_action": "No context-overlay signal-refresh sync state exists; run the bounded context refresh if preview rows exist.",
            "policy_boundary": {
                "report_mode": "read_only_diagnostic",
                "portfolio_authority": "none",
                "broker_execution_allowed": False,
                "decision_use": "avoid rerunning bounded context-overlay signal refresh when the persisted refresh is already current",
            },
        }
    state = sync_state.get("state") if isinstance(sync_state.get("state"), dict) else {}
    diagnostics = state.get("diagnostics") if isinstance(state.get("diagnostics"), dict) else {}
    state_asof = pd.to_datetime(
        state.get("asof_date") or diagnostics.get("asof_date"),
        utc=True,
        errors="coerce",
    )
    state_asof = None if pd.isna(state_asof) else state_asof.normalize()
    state_signal_rows = int(state.get("signal_rows") or 0)
    state_target_rows = int(state.get("target_rows") or 0)
    status_text = str(sync_state.get("status") or "").strip().lower()
    authority_contract = state.get("authority_contract") if isinstance(state.get("authority_contract"), dict) else {}
    broker_allowed = _boolish(state.get("broker_execution_allowed") if "broker_execution_allowed" in state else authority_contract.get("broker_execution_allowed"))
    portfolio_authority = str(state.get("portfolio_authority") or authority_contract.get("portfolio_authority") or "").strip().lower()
    asof_matches = expected_asof is None or (state_asof is not None and state_asof == expected_asof)
    row_counts_cover_preview = state_signal_rows >= preview_signal_rows and state_target_rows >= preview_target_rows
    authority_safe = not broker_allowed and portfolio_authority in {"", "none"}
    current = bool(status_text == "ok" and asof_matches and row_counts_cover_preview and authority_safe)
    return {
        "status": "current" if current else "stale_or_mismatched",
        "source_name": CONTEXT_OVERLAY_SIGNAL_REFRESH_SYNC_SOURCE,
        "expected_asof_date": expected_asof_iso,
        "state_asof_date": None if state_asof is None else state_asof.isoformat(),
        "asof_matches": bool(asof_matches),
        "state_status": status_text,
        "state_signal_rows": state_signal_rows,
        "state_target_rows": state_target_rows,
        "preview_signal_rows": preview_signal_rows,
        "preview_target_rows": preview_target_rows,
        "row_counts_cover_preview": bool(row_counts_cover_preview),
        "authority_safe": bool(authority_safe),
        "current_for_preview": bool(current),
        "signal_refresh_recommended": bool(preview_signal_rows > 0 and not current),
        "last_success_at": None if pd.isna(pd.to_datetime(sync_state.get("last_success_at"), utc=True, errors="coerce")) else pd.to_datetime(sync_state.get("last_success_at"), utc=True, errors="coerce").isoformat(),
        "last_item_ts": None if pd.isna(pd.to_datetime(sync_state.get("last_item_ts"), utc=True, errors="coerce")) else pd.to_datetime(sync_state.get("last_item_ts"), utc=True, errors="coerce").isoformat(),
        "operator_action": (
            "Bounded context-overlay signal refresh is already current for the preview; do not rerun it before full advisory."
            if current
            else "Run bounded context-overlay signal refresh before full advisory if preview rows exist."
        ),
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "avoid rerunning bounded context-overlay signal refresh when the persisted refresh is already current",
        },
    }


def load_context_overlay_signal_refresh_state(
    *,
    context_overlay_refresh_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return summarize_context_overlay_signal_refresh_state(
            load_sync_state(CONTEXT_OVERLAY_SIGNAL_REFRESH_SYNC_SOURCE),
            context_overlay_refresh_preview=context_overlay_refresh_preview,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_context_signal_refresh_state_failed",
            source=CONTEXT_OVERLAY_SIGNAL_REFRESH_SYNC_SOURCE,
            severity="warn",
            reason="Recommendation diagnostics could not inspect context-overlay signal-refresh sync state.",
            error=exc,
            metadata={},
        )
        return summarize_context_overlay_signal_refresh_state(None, context_overlay_refresh_preview=context_overlay_refresh_preview)


def context_watchlist_reconcile_command(asof_date: Any = None) -> str:
    parsed = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date is not None else None
    if parsed is not None and not pd.isna(parsed):
        return (
            "python -m advisory.watchlist_builder "
            f"--date {parsed.date().isoformat()} --setup CONTEXT_OVERLAY_WATCH --rebuild --skip-if-current"
        )
    return "python -m advisory.watchlist_builder --setup CONTEXT_OVERLAY_WATCH --rebuild --skip-if-current"


def context_overlay_signal_refresh_command(asof_date: Any = None) -> str:
    parsed = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date is not None else None
    if parsed is not None and not pd.isna(parsed):
        return (
            "python -m advisory.signal_refresh --from-context-overlays "
            f"--asof-date {parsed.date().isoformat()} --limit 50 --format text"
        )
    return "python -m advisory.signal_refresh --from-context-overlays --limit 50 --format text"


def technical_context_refresh_command(asof_date: Any = None, symbols: list[str] | None = None) -> str:
    parsed = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date is not None else None
    clean_symbols = sorted({str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()})
    if parsed is not None and not pd.isna(parsed) and clean_symbols:
        date_arg = parsed.date().isoformat()
        return (
            "python -m advisory.technical_features "
            "--targeted-context-refresh "
            f"--from-date {date_arg} "
            f"--to-date {date_arg} "
            "--symbols "
            + " ".join(clean_symbols)
        )
    return ""


def action_recommender_refresh_command(asof_date: Any = None) -> str:
    parsed = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date is not None else None
    if parsed is not None and not pd.isna(parsed):
        return f"python -m advisory.action_recommender --date {parsed.date().isoformat()} --format text"
    return "python -m advisory.action_recommender --format text"


def _date_from_command_arg(command: Any, flag: str = "--date") -> pd.Timestamp | None:
    parts = str(command or "").strip().split()
    for idx, part in enumerate(parts[:-1]):
        if part == flag:
            parsed = pd.to_datetime(parts[idx + 1], utc=True, errors="coerce")
            return None if pd.isna(parsed) else parsed.normalize()
    return None


def recommendation_diagnostics_command(asof_date: Any = None, *, output_format: str = "text") -> str:
    parsed = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date is not None else None
    fmt = "json" if str(output_format or "").strip().lower() == "json" else "text"
    if parsed is not None and not pd.isna(parsed):
        return f"python -m advisory.recommendation_diagnostics --asof-date {parsed.date().isoformat()} --format {fmt}"
    return f"python -m advisory.recommendation_diagnostics --format {fmt}"


def advisory_readiness_command_asof(candidate_source_availability: dict[str, Any] | None = None, *, asof_date: Any = None) -> pd.Timestamp | None:
    source = candidate_source_availability if isinstance(candidate_source_availability, dict) else {}
    for value in [
        source.get("latest_trading_asof_date"),
        source.get("candidate_lookup_asof_date"),
        source.get("requested_asof_date"),
        asof_date,
    ]:
        parsed = pd.to_datetime(value, utc=True, errors="coerce") if value is not None else None
        if parsed is not None and not pd.isna(parsed):
            return parsed.normalize()
    return None


def advisory_rerun_command(candidate_source_availability: dict[str, Any] | None = None, *, asof_date: Any = None) -> str:
    """Return the safest advisory rerun command for a diagnostic context."""

    parsed = advisory_readiness_command_asof(candidate_source_availability, asof_date=asof_date)
    if parsed is not None:
        return f"./all_advisory.sh --date {parsed.date().isoformat()}"
    return "./all_advisory.sh"


def default_latest_advisory_rerun_command() -> str:
    """Return the default current-date advisory rerun command used for stale artifact cleanup.

    This deliberately avoids DB lookups: in-memory diagnostics may be running precisely
    because persisted source rows are stale or unavailable. The shell wrapper itself
    resolves the latest trading day when no explicit date is supplied; for stale
    artifact reports we pin to the previous market-calendar date so automation and
    text reports do not accidentally rerun the old artifact date.
    """

    market_date = pd.Timestamp.now(tz="Asia/Kolkata").date()
    previous_market_date = market_date - pd.Timedelta(days=1)
    return f"./all_advisory.sh --date {previous_market_date.isoformat()}"


def technical_threshold_calibration_command(asof_date: Any = None, *, lookback_days: int = 120) -> str:
    """Return a bounded research-only calibration command for the diagnostic date.

    The command is deliberately dry-run and date-bounded. It should be used to
    study realised outcomes of technical blockers, not to mutate production
    thresholds or portfolio state.
    """

    parsed = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date is not None else None
    base = "python -m advisory.technical_threshold_calibration --dry-run"
    if parsed is not None and not pd.isna(parsed):
        normalized = parsed.normalize()
        start = (normalized - pd.Timedelta(days=max(1, int(lookback_days)))).date().isoformat()
        end = normalized.date().isoformat()
        base = f"{base} --from-date {start} --to-date {end}"
    return f"{base} --horizons 5 10 20 --max-configs 512 --progress-every 128"


def load_latest_trigger_near_miss_research(
    *,
    min_signals: int = TECHNICAL_THRESHOLD_MIN_SIGNALS,
    return_threshold: float = 0.03,
    min_hit_rate: float = 0.5,
) -> dict[str, Any]:
    """Read persisted trigger near-miss calibration evidence for diagnostics.

    This is research-only. It helps operators decide whether a strict technical
    trigger deserves more study; it never changes thresholds, actions, portfolio
    rows, or broker behaviour.
    """

    authority = {
        "authority": "research_only_manual_review",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "policy_effect": "diagnostic_only_no_threshold_change",
    }
    try:
        columns = _table_columns(TECHNICAL_THRESHOLD_SUMMARY_TABLE)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            source=TECHNICAL_THRESHOLD_SUMMARY_TABLE,
            fallback_type="recommendation_diagnostics_trigger_near_miss_columns_load_failed",
            severity="warn",
            reason="Recommendation diagnostics could not inspect trigger near-miss calibration columns.",
            error=exc,
        )
        return {
            "status": "error",
            **authority,
            "error": f"{type(exc).__name__}: {exc}",
            "near_miss_rows": 0,
            "classification_counts": {},
            "top_relaxation_candidates": [],
            "top_do_not_relax": [],
            "needs_more_label_near_misses": [],
        }
    if not columns:
        return {
            "status": "missing_calibration_summary_table",
            **authority,
            "near_miss_rows": 0,
            "classification_counts": {},
            "top_relaxation_candidates": [],
            "top_do_not_relax": [],
            "needs_more_label_near_misses": [],
        }
    required_columns = {"evaluated_at", "horizon_days", "archetype_breakdown_json"}
    missing_columns = sorted(required_columns - columns)
    if missing_columns:
        return {
            "status": "missing_near_miss_columns",
            **authority,
            "missing_columns": missing_columns,
            "near_miss_rows": 0,
            "classification_counts": {},
            "top_relaxation_candidates": [],
            "top_do_not_relax": [],
            "needs_more_label_near_misses": [],
        }
    try:
        latest_df = sql_to_df(
            f"SELECT MAX(evaluated_at) AS latest_evaluated_at FROM {TECHNICAL_THRESHOLD_SUMMARY_TABLE}",
            retries=2,
            statement_timeout_ms=10000,
        )
        latest_value = None if latest_df.empty else latest_df["latest_evaluated_at"].iloc[0]
        latest_evaluated_at = pd.to_datetime(latest_value, utc=True, errors="coerce")
        if pd.isna(latest_evaluated_at):
            return {
                "status": "missing_latest_evaluated_at",
                **authority,
                "near_miss_rows": 0,
                "classification_counts": {},
                "top_relaxation_candidates": [],
                "top_do_not_relax": [],
                "needs_more_label_near_misses": [],
            }
        df = sql_to_df(
            f"""
            SELECT evaluated_at, horizon_days, archetype_breakdown_json
            FROM {TECHNICAL_THRESHOLD_SUMMARY_TABLE}
            WHERE evaluated_at = %(latest)s
            ORDER BY horizon_days
            """,
            params={"latest": latest_evaluated_at},
            retries=2,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            source=TECHNICAL_THRESHOLD_SUMMARY_TABLE,
            fallback_type="recommendation_diagnostics_trigger_near_miss_load_failed",
            severity="warn",
            reason="Recommendation diagnostics could not load trigger near-miss calibration evidence.",
            error=exc,
        )
        return {
            "status": "error",
            **authority,
            "error": f"{type(exc).__name__}: {exc}",
            "near_miss_rows": 0,
            "classification_counts": {},
            "top_relaxation_candidates": [],
            "top_do_not_relax": [],
            "needs_more_label_near_misses": [],
        }

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in df.to_dict(orient="records") if not df.empty else []:
        payload = _jsonish(row.get("archetype_breakdown_json"), {})
        payload = payload if isinstance(payload, dict) else {}
        horizon = int(row.get("horizon_days") or payload.get("horizon_days") or 0)
        for item in payload.get("trigger_near_miss_breakdown") or []:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("near_miss_archetype") or "unknown"),
                str(item.get("near_miss_blocker_key") or "none"),
            )
            bucket = buckets.setdefault(
                key,
                {
                    "near_miss_archetype": key[0],
                    "near_miss_blocker_key": key[1],
                    "near_miss_blocker_codes": item.get("near_miss_blocker_codes")
                    if isinstance(item.get("near_miss_blocker_codes"), list)
                    else [],
                    "horizons": [],
                    "signal_count": 0,
                    "_return_weighted_sum": 0.0,
                    "_return_weight": 0,
                    "_hit_weighted_sum": 0.0,
                    "_hit_weight": 0,
                    "sample_blockers": item.get("sample_blockers") if isinstance(item.get("sample_blockers"), list) else [],
                },
            )
            if horizon and horizon not in bucket["horizons"]:
                bucket["horizons"].append(horizon)
            signal_count = int(item.get("signal_count") or 0)
            bucket["signal_count"] += signal_count
            avg_return = pd.to_numeric(item.get("avg_return_after_cost"), errors="coerce")
            if not pd.isna(avg_return) and signal_count > 0:
                bucket["_return_weighted_sum"] += float(avg_return) * signal_count
                bucket["_return_weight"] += signal_count
            hit_rate = pd.to_numeric(item.get("hit_rate_after_cost"), errors="coerce")
            if not pd.isna(hit_rate) and signal_count > 0:
                bucket["_hit_weighted_sum"] += float(hit_rate) * signal_count
                bucket["_hit_weight"] += signal_count

    rows: list[dict[str, Any]] = []
    for bucket in buckets.values():
        avg_return = (
            None
            if int(bucket["_return_weight"]) == 0
            else round(float(bucket["_return_weighted_sum"] / bucket["_return_weight"]), 6)
        )
        hit_rate = (
            None
            if int(bucket["_hit_weight"]) == 0
            else round(float(bucket["_hit_weighted_sum"] / bucket["_hit_weight"]), 6)
        )
        signal_count = int(bucket["signal_count"])
        if signal_count < int(min_signals) or avg_return is None or hit_rate is None:
            classification = "needs_more_matured_labels"
        elif avg_return >= float(return_threshold) and hit_rate >= float(min_hit_rate):
            classification = "potential_trigger_relaxation_candidate"
        elif avg_return <= 0.0 or hit_rate < 0.4:
            classification = "do_not_relax_negative_or_weak"
        else:
            classification = "mixed_or_marginal_requires_review"
        rows.append(
            {
                "near_miss_archetype": bucket["near_miss_archetype"],
                "near_miss_blocker_key": bucket["near_miss_blocker_key"],
                "near_miss_blocker_codes": bucket["near_miss_blocker_codes"],
                "horizons": sorted(bucket["horizons"]),
                "signal_count": signal_count,
                "avg_return_after_cost": avg_return,
                "hit_rate_after_cost": hit_rate,
                "classification": classification,
                "sample_blockers": bucket["sample_blockers"],
                **authority,
            }
        )
    rows.sort(
        key=lambda item: (
            item["classification"] == "potential_trigger_relaxation_candidate",
            int(item.get("signal_count") or 0),
            float(item.get("avg_return_after_cost") or -999.0),
        ),
        reverse=True,
    )
    classification_counts = dict(pd.Series([item["classification"] for item in rows]).value_counts().to_dict()) if rows else {}
    return {
        "status": "ok" if rows else "no_near_miss_rows",
        **authority,
        "latest_evaluated_at": latest_evaluated_at.isoformat(),
        "min_signals": int(min_signals),
        "return_threshold": float(return_threshold),
        "min_hit_rate": float(min_hit_rate),
        "near_miss_rows": len(rows),
        "classification_counts": classification_counts,
        "top_relaxation_candidates": [item for item in rows if item["classification"] == "potential_trigger_relaxation_candidate"][:10],
        "top_do_not_relax": [item for item in rows if item["classification"] == "do_not_relax_negative_or_weak"][:10],
        "needs_more_label_near_misses": [item for item in rows if item["classification"] == "needs_more_matured_labels"][:10],
        "mixed_or_marginal_near_misses": [item for item in rows if item["classification"] == "mixed_or_marginal_requires_review"][:10],
    }


def build_technical_threshold_research_plan(
    *,
    candidate_source_availability: dict[str, Any] | None = None,
    technical_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the operator runbook for no-BUY technical-threshold research.

    This stays read-only: it only determines whether the operator should
    regenerate candidate rows with the new blocker contract before running
    realised-outcome calibration, and it reminds operators that blocker outcome
    summaries remain empty until forward labels mature.
    """

    candidate_source_availability = (
        candidate_source_availability if isinstance(candidate_source_availability, dict) else {}
    )
    technical_readiness = technical_readiness if isinstance(technical_readiness, dict) else {}
    trigger_blockers = technical_readiness.get("technical_trigger_blockers")
    buy_blockers = technical_readiness.get("buy_readiness_blockers")
    trigger_blockers = trigger_blockers if isinstance(trigger_blockers, dict) else {}
    buy_blockers = buy_blockers if isinstance(buy_blockers, dict) else {}
    trigger_status = str(trigger_blockers.get("status") or "").strip()
    buy_status = str(buy_blockers.get("status") or "").strip()
    contract_available = trigger_status.endswith("_available") or buy_status.endswith("_available")
    contract_missing = (
        trigger_status.endswith("_missing")
        or buy_status.endswith("_missing")
        or (not trigger_status and not buy_status)
    )
    reconstructed_contract_count = max(
        int(trigger_blockers.get("candidate_count_with_reconstructed_contract") or 0),
        int(buy_blockers.get("candidate_count_with_reconstructed_contract") or 0),
    )
    asof_value = (
        candidate_source_availability.get("candidate_lookup_asof_date")
        or candidate_source_availability.get("latest_trading_asof_date")
        or candidate_source_availability.get("latest_candidate_asof_date")
        or candidate_source_availability.get("requested_asof_date")
    )
    parsed_asof = pd.to_datetime(asof_value, utc=True, errors="coerce") if asof_value is not None else None
    if parsed_asof is not None and pd.isna(parsed_asof):
        parsed_asof = None
    diagnostic_command = recommendation_diagnostics_command(parsed_asof, output_format="json")
    calibration_command = technical_threshold_calibration_command(parsed_asof)
    if contract_missing and not contract_available:
        rerun_command = (
            advisory_rerun_command(candidate_source_availability, asof_date=parsed_asof)
            if parsed_asof is not None
            else "./all_advisory.sh"
        )
        return {
            "status": "needs_candidate_regeneration_for_blocker_contract",
            "ready_for_calibration": False,
            "eligible_for_threshold_research": False,
            "reason": (
                "Constructive candidates predate the technical trigger/buy-readiness blocker contract. "
                "Regenerate rule/advisory candidates before interpreting blocker distributions."
            ),
            "candidate_asof_date": None if parsed_asof is None else parsed_asof.date().isoformat(),
            "trigger_blocker_status": trigger_status or "missing",
            "buy_readiness_status": buy_status or "missing",
            "policy_effect": "diagnostic_only_no_threshold_change",
            "label_maturity_note": (
                "After regeneration, realised blocker outcomes can still be empty until forward OHLCV labels mature "
                "for the tested horizons."
            ),
            "next_commands": [
                {
                    "command": rerun_command,
                    "purpose": "Regenerate candidates with current technical blocker diagnostics before threshold research.",
                },
                {
                    "command": diagnostic_command,
                    "purpose": "Confirm trigger/buy-readiness blocker contracts are present in recommendation diagnostics.",
                },
            ],
        }
    reconstructed_contracts_used = reconstructed_contract_count > 0
    trigger_near_miss_research = load_latest_trigger_near_miss_research()
    return {
        "status": (
            "ready_for_bounded_calibration_with_reconstructed_contract_caveat"
            if reconstructed_contracts_used
            else "ready_for_bounded_calibration_with_maturity_caveat"
        ),
        "ready_for_calibration": True,
        "eligible_for_threshold_research": True,
        "reason": (
            "Technical blocker contracts are present. Run bounded dry-run calibration only as realised-outcome research; "
            "do not promote threshold changes unless blocker outcomes are mature and improve after costs."
            if not reconstructed_contracts_used
            else (
                "Technical blocker contracts are available because recommendation diagnostics reconstructed them from "
                "point-in-time technical features. Use bounded dry-run calibration only as directional research, and "
                "regenerate advisory candidates with current code before any promotion review."
            )
        ),
        "candidate_asof_date": None if parsed_asof is None else parsed_asof.date().isoformat(),
        "trigger_blocker_status": trigger_status or "missing",
        "buy_readiness_status": buy_status or "missing",
        "diagnostic_reconstructed_contract_count": int(reconstructed_contract_count),
        "candidate_regeneration_recommended_before_promotion": bool(reconstructed_contracts_used),
        "policy_effect": "diagnostic_only_no_threshold_change",
        "trigger_near_miss_research": trigger_near_miss_research,
        "trigger_near_miss_candidate_count": len(trigger_near_miss_research.get("top_relaxation_candidates") or []),
        "trigger_near_miss_do_not_relax_count": len(trigger_near_miss_research.get("top_do_not_relax") or []),
        "trigger_near_miss_needs_more_label_count": len(trigger_near_miss_research.get("needs_more_label_near_misses") or []),
        "label_maturity_note": (
            "If calibration returns zero blocker_breakdown rows, forward labels have not matured for rows carrying "
            "the blocker contract; wait for data catch-up instead of tuning policy."
            if not reconstructed_contracts_used
            else (
                "Reconstructed contracts are diagnostic-only. If calibration returns zero blocker_breakdown rows, "
                "forward labels have not matured; if it returns useful blocker outcomes, rerun advisory/rules so "
                "persisted candidates carry native blocker contracts before any config promotion review."
            )
        ),
        "next_commands": [
            {
                "command": diagnostic_command,
                "purpose": "Inspect constructive candidates, trigger blockers, and buy-readiness blockers for the diagnostic date.",
            },
            {
                "command": calibration_command,
                "purpose": "Research realised outcomes of technical blocker thresholds without changing live policy, portfolio rows, or broker behavior.",
            },
        ],
    }


def bounded_authoritative_reconciliation_command(asof_date: Any = None) -> str:
    """Return a faster rules-through-actions rerun after prerequisites are current."""

    parsed = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date is not None else None
    date_arg = f" --date {parsed.date().isoformat()}" if parsed is not None and not pd.isna(parsed) else ""
    return (
        "python -m advisory.pipeline"
        f"{date_arg}"
        " --start-at rules --stop-at actions"
        " --skip-peer-sync --skip-intraday --skip-rule-snapshot-refresh --skip-intraday-prefetch"
        " --include-lifecycle"
    )


def summarize_advisory_rerun_readiness(
    *,
    candidate_source_availability: dict[str, Any] | None = None,
    suppression_counts: Counter[str] | dict[str, int] | None = None,
    context_overlay_source_diagnostics: dict[str, Any] | None = None,
    context_watchlist_intake: dict[str, Any] | None = None,
    context_overlay_refresh_preview: dict[str, Any] | None = None,
    context_overlay_signal_refresh_state: dict[str, Any] | None = None,
    causal_memory_refresh_preview: dict[str, Any] | None = None,
    post_advisory_activity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_source_availability = candidate_source_availability if isinstance(candidate_source_availability, dict) else {}
    suppression_counts = suppression_counts if isinstance(suppression_counts, (Counter, dict)) else {}
    context_overlay_source_diagnostics = context_overlay_source_diagnostics if isinstance(context_overlay_source_diagnostics, dict) else {}
    context_watchlist_intake = context_watchlist_intake if isinstance(context_watchlist_intake, dict) else {}
    context_overlay_refresh_preview = context_overlay_refresh_preview if isinstance(context_overlay_refresh_preview, dict) else {}
    context_overlay_signal_refresh_state = context_overlay_signal_refresh_state if isinstance(context_overlay_signal_refresh_state, dict) else {}
    causal_memory_refresh_preview = causal_memory_refresh_preview if isinstance(causal_memory_refresh_preview, dict) else {}
    post_advisory_activity = post_advisory_activity if isinstance(post_advisory_activity, dict) else {}
    command_asof = advisory_readiness_command_asof(candidate_source_availability)
    preview_asof = pd.to_datetime(
        context_overlay_refresh_preview.get("requested_asof_date")
        or context_overlay_refresh_preview.get("asof_date")
        or (context_overlay_refresh_preview.get("diagnostics") or {}).get("asof_date"),
        utc=True,
        errors="coerce",
    )
    preview_asof = None if pd.isna(preview_asof) else preview_asof.normalize()
    post_action_refresh_asof = _date_from_command_arg(post_advisory_activity.get("action_refresh_command"))
    refresh_command_asof = command_asof or preview_asof or post_action_refresh_asof
    advisory_command = advisory_rerun_command(candidate_source_availability)
    diagnostic_command = recommendation_diagnostics_command(command_asof)

    candidate_status = str(candidate_source_availability.get("status") or "").strip()
    context_source_status = str(context_overlay_source_diagnostics.get("status") or "").strip()
    context_watchlist_status = str(context_watchlist_intake.get("status") or "").strip()
    feature_blocked_count = int(suppression_counts.get("feature_freshness_blocked", 0) or 0)
    context_watchlist_active_count = int(context_watchlist_intake.get("active_watch_count") or 0)
    if "current_for_reconcile" in context_watchlist_intake:
        context_watchlist_current = bool(context_watchlist_intake.get("current_for_reconcile"))
    else:
        context_watchlist_current = bool(
            context_watchlist_status in {"context_watchlist_active_watch_pressure", "context_watchlist_breakout_pressure"}
            and context_watchlist_active_count > 0
        )
    context_preview_signals = int(context_overlay_refresh_preview.get("signal_rows") or 0)
    context_positive_watch_targets = int(context_overlay_refresh_preview.get("positive_watch_target_rows") or 0)
    context_signal_refresh_current = bool(context_overlay_signal_refresh_state.get("current_for_preview"))
    context_signal_refresh_recommended = bool(context_preview_signals > 0 and not context_signal_refresh_current)
    causal_memory_signals = int(causal_memory_refresh_preview.get("signal_rows") or 0)
    action_refresh_pending = bool(post_advisory_activity.get("action_refresh_recommended")) and int(
        post_advisory_activity.get("action_refresh_candidate_count") or 0
    ) > 0
    candidate_source_blocked = candidate_status in {
        "candidate_rows_missing_for_asof_latest_exists",
        "candidate_rows_missing",
        "candidate_source_unavailable",
        "candidate_source_availability_failed",
    }
    context_sources_missing = context_source_status in {
        "no_context_overlay_source_rows",
        "context_overlay_rows_exist_but_none_active_authorized",
    }
    blockers: list[str] = []
    if feature_blocked_count > 0:
        blockers.append("feature_freshness_blocked")
    if context_sources_missing:
        blockers.append(context_source_status)

    commands: list[dict[str, str]] = []
    if context_signal_refresh_recommended:
        commands.append(
            {
                "command": context_overlay_signal_refresh_command(
                    refresh_command_asof
                ),
                "purpose": "Persist review-only context-overlay Action Queue rows before the long advisory reconciliation.",
            }
        )
    context_watchlist_reconcile_needed = bool(context_positive_watch_targets > 0 and not context_watchlist_current)
    if context_watchlist_reconcile_needed:
        commands.append(
            {
                "command": context_watchlist_reconcile_command(
                    refresh_command_asof
                ),
                "purpose": "Persist context-overlay watchlist rows only, so news/macro/bhavcopy watch pressure is visible without the full advisory run.",
            }
        )
    if causal_memory_signals > 0:
        commands.append(
            {
                "command": str(
                    causal_memory_refresh_preview.get("recommended_command")
                    or "python -m advisory.signal_refresh --from-causal-memory --limit 25 --format text"
                ),
                "purpose": "Persist review-only causal-memory Action Queue rows before the long advisory reconciliation.",
            }
        )
    if action_refresh_pending:
        commands.append(
            {
                "command": action_recommender_refresh_command(refresh_command_asof),
                "purpose": "Refresh review-only Action Queue visibility from watcher evidence before full advisory reconciliation.",
            }
        )

    if blockers:
        status = "refresh_data_before_advisory"
        operator_action = (
            "Run complete_data.sh before the long advisory rerun because prerequisite feature/context source evidence is missing, stale, or blocked."
        )
        commands.append({"command": "./complete_data.sh", "purpose": "Refresh missing/stale prerequisite data and context overlay sources."})
        commands.append({"command": advisory_command, "purpose": "Regenerate rule candidates and authoritative advisory decisions after data refresh."})
    elif commands:
        status = "bounded_refresh_before_full_advisory"
        operator_action = (
            "Run bounded review-only refresh command(s) first for fresher Action Queue visibility, then run all_advisory.sh for authoritative reconciliation."
        )
        commands.append({"command": advisory_command, "purpose": "Regenerate authoritative risk, portfolio, lifecycle, and action decisions."})
    elif candidate_source_blocked:
        status = "ready_for_full_advisory"
        operator_action = "Prerequisite source checks do not show a data-refresh blocker; run all_advisory.sh for the relevant trading date to regenerate stale/missing candidates."
        commands.append({"command": advisory_command, "purpose": "Regenerate rule-engine candidates and consolidated recommendations."})
    else:
        status = "full_advisory_not_required_by_readiness"
        operator_action = "Candidate and prerequisite source evidence do not require a full advisory rerun from this readiness check alone."
        commands.append({"command": recommendation_diagnostics_command(output_format="json"), "purpose": "Inspect detailed no-BUY evidence before rerunning long workflows."})
    commands.append(
        {
            "command": diagnostic_command,
            "purpose": "Re-check the first actionable no-BUY cause after the selected command finishes.",
        }
    )

    seen: set[str] = set()
    deduped_commands: list[dict[str, str]] = []
    for command in commands:
        text = str(command.get("command") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped_commands.append(command)
    return {
        "status": status,
        "blockers": blockers,
        "candidate_source_status": candidate_status,
        "candidate_lookup_asof_date": candidate_source_availability.get("candidate_lookup_asof_date"),
        "latest_candidate_trading_age_days": candidate_source_availability.get("latest_candidate_trading_age_days"),
        "feature_freshness_blocked_count": feature_blocked_count,
        "context_overlay_source_status": context_source_status,
        "context_overlay_active_authorized_rows_total": int(context_overlay_source_diagnostics.get("active_authorized_rows_total") or 0),
        "context_watchlist_status": context_watchlist_status,
        "context_watchlist_active_watch_count": context_watchlist_active_count,
        "context_watchlist_current": context_watchlist_current,
        "context_watchlist_freshness_status": context_watchlist_intake.get("context_watchlist_freshness_status"),
        "context_watchlist_sync_asof_matches": context_watchlist_intake.get("sync_asof_matches"),
        "context_watchlist_fresh_for_signal_refresh": context_watchlist_intake.get("watchlist_fresh_for_signal_refresh"),
        "context_watchlist_reconcile_recommended": context_watchlist_reconcile_needed,
        "context_overlay_signal_refresh_status": context_overlay_signal_refresh_state.get("status"),
        "context_overlay_signal_refresh_current": context_signal_refresh_current,
        "context_overlay_signal_refresh_recommended": context_signal_refresh_recommended,
        "context_overlay_preview_signal_rows": context_preview_signals,
        "context_overlay_positive_watch_target_rows": context_positive_watch_targets,
        "causal_memory_preview_signal_rows": causal_memory_signals,
        "post_advisory_action_refresh_pending": bool(action_refresh_pending),
        "recommended_commands": deduped_commands,
        "operator_action": operator_action,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "choose the next operator command before interpreting missing BUYs or changing regime/context/threshold policy",
        },
    }


def align_post_advisory_activity_to_readiness(
    post_advisory_activity: dict[str, Any] | None,
    advisory_rerun_readiness: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep post-advisory fast-refresh display aligned with the selected runbook date."""

    post = dict(post_advisory_activity) if isinstance(post_advisory_activity, dict) else {}
    if not bool(post.get("action_refresh_recommended")):
        return post
    readiness = advisory_rerun_readiness if isinstance(advisory_rerun_readiness, dict) else {}
    commands = readiness.get("recommended_commands") if isinstance(readiness.get("recommended_commands"), list) else []
    for item in commands:
        if not isinstance(item, dict):
            continue
        command = str(item.get("command") or "").strip()
        if "advisory.action_recommender" in command:
            existing = str(post.get("action_refresh_command") or "").strip()
            if "--date" not in command and "--date" in existing:
                return post
            post["action_refresh_command"] = command
            post["action_refresh_command_source"] = "advisory_rerun_readiness"
            return post
    return post


def align_context_overlay_preview_to_readiness(
    context_overlay_refresh_preview: dict[str, Any] | None,
    advisory_rerun_readiness: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep context-overlay preview command aligned with the selected trading-date runbook."""

    preview = dict(context_overlay_refresh_preview) if isinstance(context_overlay_refresh_preview, dict) else {}
    readiness = advisory_rerun_readiness if isinstance(advisory_rerun_readiness, dict) else {}
    commands = readiness.get("recommended_commands") if isinstance(readiness.get("recommended_commands"), list) else []
    for item in commands:
        if not isinstance(item, dict):
            continue
        command = str(item.get("command") or "").strip()
        if "advisory.signal_refresh" in command and "--from-context-overlays" in command:
            original = str(preview.get("recommended_command") or "").strip()
            if original and original != command:
                preview["original_recommended_command"] = original
                preview["recommended_command_aligned_from"] = "advisory_rerun_readiness"
            preview["recommended_command"] = command
            return preview
    return preview


def apply_advisory_rerun_readiness_to_primary_cause(
    primary_cause: dict[str, Any] | None,
    advisory_rerun_readiness: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(primary_cause, dict):
        return {}
    if not isinstance(advisory_rerun_readiness, dict):
        return primary_cause
    readiness_commands = advisory_rerun_readiness.get("recommended_commands")
    if not isinstance(readiness_commands, list) or not readiness_commands:
        return primary_cause
    readiness_status = str(advisory_rerun_readiness.get("status") or "").strip()
    if readiness_status not in {
        "refresh_data_before_advisory",
        "bounded_refresh_before_full_advisory",
        "ready_for_full_advisory",
    }:
        return primary_cause
    current_commands = primary_cause.get("next_commands") if isinstance(primary_cause.get("next_commands"), list) else []
    current_command_texts = {
        str(item.get("command") if isinstance(item, dict) else item).strip()
        for item in current_commands
        if str(item.get("command") if isinstance(item, dict) else item).strip()
    }
    readiness_command_texts = [
        str(item.get("command") if isinstance(item, dict) else item).strip()
        for item in readiness_commands
        if str(item.get("command") if isinstance(item, dict) else item).strip()
    ]
    if readiness_command_texts == list(current_command_texts):
        return primary_cause
    enriched = {**primary_cause}
    enriched["next_commands"] = readiness_commands
    enriched["next_commands_source"] = "advisory_rerun_readiness"
    enriched["readiness_status"] = readiness_status
    enriched["readiness_operator_action"] = advisory_rerun_readiness.get("operator_action")
    readiness_action = str(advisory_rerun_readiness.get("operator_action") or "").strip()
    original_action = str(primary_cause.get("operator_action") or "").strip()
    if readiness_action and original_action and readiness_action != original_action:
        enriched["operator_action"] = f"{readiness_action} Underlying no-BUY cause: {original_action}"
    elif readiness_action:
        enriched["operator_action"] = readiness_action
    evidence = enriched.get("evidence") if isinstance(enriched.get("evidence"), dict) else {}
    enriched["evidence"] = {
        **evidence,
        "advisory_rerun_readiness": {
            "status": readiness_status,
            "blockers": advisory_rerun_readiness.get("blockers", []),
            "candidate_source_status": advisory_rerun_readiness.get("candidate_source_status"),
            "feature_freshness_blocked_count": advisory_rerun_readiness.get("feature_freshness_blocked_count"),
            "context_overlay_source_status": advisory_rerun_readiness.get("context_overlay_source_status"),
            "context_overlay_preview_signal_rows": advisory_rerun_readiness.get("context_overlay_preview_signal_rows"),
            "causal_memory_preview_signal_rows": advisory_rerun_readiness.get("causal_memory_preview_signal_rows"),
            "post_advisory_action_refresh_pending": advisory_rerun_readiness.get("post_advisory_action_refresh_pending"),
            "policy_boundary": advisory_rerun_readiness.get("policy_boundary", {}),
        },
    }
    return enriched


def _command_text(command: Any) -> str:
    return str(command.get("command") if isinstance(command, dict) else command).strip()


def _format_command_sequence(commands: list[Any]) -> str:
    texts = [_command_text(command) for command in commands]
    texts = [text for text in texts if text]
    if not texts:
        return "the relevant diagnostic command"
    return ", then ".join(f"`{text}`" for text in texts)


def align_advisory_rerun_readiness_to_primary_cause(
    advisory_rerun_readiness: dict[str, Any] | None,
    primary_no_buy_cause: dict[str, Any] | None,
) -> dict[str, Any]:
    readiness = advisory_rerun_readiness if isinstance(advisory_rerun_readiness, dict) else {}
    primary = primary_no_buy_cause if isinstance(primary_no_buy_cause, dict) else {}
    commands = primary.get("next_commands") if isinstance(primary.get("next_commands"), list) else []
    if (
        str(primary.get("next_commands_source") or "") != "context_to_entry_pipeline_then_advisory_rerun_readiness"
        or not commands
    ):
        return readiness
    aligned = {**readiness}
    aligned["status"] = "superseded_by_context_to_entry_repair"
    aligned["underlying_status"] = readiness.get("status")
    aligned["underlying_operator_action"] = readiness.get("operator_action")
    aligned["underlying_recommended_commands"] = readiness.get("recommended_commands", [])
    aligned["operator_action"] = (
        "Primary no-BUY diagnostics found a narrower context-to-entry repair chain; run those commands before falling back to generic full-advisory readiness."
    )
    aligned["recommended_commands"] = commands
    aligned["recommended_commands_source"] = "primary_no_buy_context_to_entry_commands"
    aligned["primary_no_buy_code"] = primary.get("code")
    aligned["primary_no_buy_first_command"] = _command_text(commands[0]) if commands else None
    return aligned


def _sanitize_context_to_entry_underlying_action(action: str) -> str:
    sanitized = str(action or "").strip()
    replacements = {
        "Run all_advisory.sh for the relevant trading date before interpreting missing BUYs as regime, threshold, context, or LLM behavior.": (
            "Complete the context-to-entry command sequence above before interpreting missing BUYs as regime, threshold, context, or LLM behavior."
        ),
        "Run all_advisory.sh for the relevant trading date before interpreting missing BUYs as regime, threshold, context, or LLM behavior": (
            "Complete the context-to-entry command sequence above before interpreting missing BUYs as regime, threshold, context, or LLM behavior"
        ),
        "Run all_advisory.sh and verify advisory_candidates persistence before diagnosing missing BUYs.": (
            "Complete the context-to-entry command sequence above and verify advisory_candidates persistence before diagnosing missing BUYs."
        ),
        "Run advisory before changing policy.": "Complete the context-to-entry command sequence above before changing policy.",
    }
    for old, new in replacements.items():
        sanitized = sanitized.replace(old, new)
    return sanitized


def apply_context_to_entry_commands_to_primary_cause(
    primary_cause: dict[str, Any] | None,
    context_to_entry_pipeline: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(primary_cause, dict):
        return {}
    if not isinstance(context_to_entry_pipeline, dict):
        return primary_cause
    primary_code = str(primary_cause.get("code") or "").strip()
    if primary_code not in {
        "candidate_source_stale_for_asof",
        "candidate_source_date_mismatch",
        "candidate_source_missing",
        "diagnostic_requires_advisory_rerun",
    }:
        return primary_cause
    bottleneck = str(context_to_entry_pipeline.get("bottleneck") or "").strip()
    if bottleneck not in {
        "context_watchlist_reconcile_stale",
        "context_watchlist_missing_or_mismatched",
        "context_watch_technical_feature_stale",
        "context_watch_technical_refresh_failed",
        "context_watch_hard_ohlcv_input_blockers",
    }:
        return primary_cause
    pipeline_commands = (
        context_to_entry_pipeline.get("next_commands")
        if isinstance(context_to_entry_pipeline.get("next_commands"), list)
        else []
    )
    if not pipeline_commands:
        return primary_cause
    current_commands = primary_cause.get("next_commands") if isinstance(primary_cause.get("next_commands"), list) else []
    merged_commands: list[Any] = []
    seen: set[str] = set()
    bounded_pipeline_present = any(
        _command_text(command).startswith("python -m advisory.pipeline ")
        for command in pipeline_commands
    )
    for command in [*pipeline_commands, *current_commands]:
        command_text = str(command.get("command") if isinstance(command, dict) else command).strip()
        if not command_text or command_text in seen:
            continue
        if bounded_pipeline_present and command_text.startswith("./all_advisory.sh"):
            continue
        seen.add(command_text)
        merged_commands.append(command)
    if not merged_commands:
        return primary_cause
    enriched = {**primary_cause}
    enriched["next_commands"] = merged_commands
    enriched["next_commands_source"] = "context_to_entry_pipeline_then_advisory_rerun_readiness"
    enriched["context_to_entry_bottleneck"] = bottleneck
    command_sequence = _format_command_sequence(merged_commands[:2])
    pipeline_action = str(context_to_entry_pipeline.get("operator_action") or "").strip()
    original_action = str(primary_cause.get("operator_action") or "").strip()
    underlying_action = (
        original_action.split("Underlying no-BUY cause:", 1)[-1].strip()
        if "Underlying no-BUY cause:" in original_action
        else original_action
    )
    underlying_action = _sanitize_context_to_entry_underlying_action(underlying_action)
    if pipeline_action and underlying_action and pipeline_action not in underlying_action:
        enriched["operator_action"] = (
            f"{pipeline_action} Run the bounded command sequence in order: {command_sequence}. "
            "Rerun diagnostics before changing thresholds, regime/context policy, or LLM policy. "
            f"Underlying no-BUY cause: {underlying_action}"
        )
    elif pipeline_action:
        enriched["operator_action"] = (
            f"{pipeline_action} Run the bounded command sequence in order: {command_sequence}. "
            "Rerun diagnostics before changing thresholds, regime/context policy, or LLM policy."
        )
    evidence = enriched.get("evidence") if isinstance(enriched.get("evidence"), dict) else {}
    enriched["evidence"] = {
        **evidence,
        "context_to_entry_pipeline": {
            "status": context_to_entry_pipeline.get("status"),
            "bottleneck": bottleneck,
            "active_context_watchlist_rows": context_to_entry_pipeline.get("active_context_watchlist_rows"),
            "context_watchlist_current": context_to_entry_pipeline.get("context_watchlist_current"),
            "context_watch_technical_precheck_status": context_to_entry_pipeline.get("context_watch_technical_precheck_status"),
            "context_watch_constructive_precheck_count": context_to_entry_pipeline.get("context_watch_constructive_precheck_count"),
            "constructive_candidate_count": context_to_entry_pipeline.get("constructive_candidate_count"),
            "confirmed_entry_candidate_count": context_to_entry_pipeline.get("confirmed_entry_candidate_count"),
            "final_positive_action_count": context_to_entry_pipeline.get("final_positive_action_count"),
            "policy_boundary": context_to_entry_pipeline.get("policy_boundary", {}),
        },
    }
    return enriched


def summarize_causal_memory_refresh_preview(preview: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(preview, dict):
        return {
            "status": "not_loaded",
            "operator_action": "Causal-memory refresh preview was not loaded for this in-memory diagnostic call.",
            "policy_boundary": {
                "report_mode": "read_only_diagnostic",
                "portfolio_authority": "none",
                "broker_execution_allowed": False,
                "decision_use": "preview whether causal event memory can create review-only Action Queue rows before a long advisory rerun",
            },
        }
    target_rows = int(preview.get("target_rows") or 0)
    signal_rows = int(preview.get("signal_rows") or 0)
    affected_symbols = [
        str(value).strip().upper()
        for value in (preview.get("affected_symbols") or [])
        if str(value).strip()
    ]
    diagnostics = preview.get("diagnostics") if isinstance(preview.get("diagnostics"), dict) else {}
    asof_value = preview.get("requested_asof_date") or diagnostics.get("asof_date") or diagnostics.get("asof_cutoff")
    asof_ts = pd.to_datetime(asof_value, utc=True, errors="coerce") if asof_value is not None else None
    asof_text = None if asof_ts is None or pd.isna(asof_ts) else asof_ts.date().isoformat()
    command = "python -m advisory.signal_refresh --from-causal-memory --limit 25 --format text"
    if asof_text:
        command = f"python -m advisory.signal_refresh --from-causal-memory --asof-date {asof_text} --limit 25 --format text"
    if signal_rows > 0:
        status = "causal_memory_refresh_would_emit_review_only_signals"
        operator_action = (
            "A read-only preview shows candidate-helpful causal event memory can emit review-only Action Queue signals now; "
            "run the bounded causal-memory signal refresh before the long full-advisory reconciliation."
        )
    elif target_rows > 0:
        status = "causal_memory_targets_without_signal_rows"
        operator_action = "Causal-memory targets exist but did not become signal rows; inspect diagnostics before treating memory evidence as absent."
    else:
        status = "no_causal_memory_refresh_targets"
        operator_action = "Causal-memory refresh preview found no fresh candidate-helpful symbol-scoped memory targets."
    return {
        "status": status,
        "target_rows": target_rows,
        "signal_rows": signal_rows,
        "affected_symbol_count": int(len(affected_symbols)),
        "affected_symbols": affected_symbols[:50],
        "memory_table": diagnostics.get("memory_table"),
        "summary_table": diagnostics.get("summary_table"),
        "min_decayed_pressure_score": diagnostics.get("min_decayed_pressure_score"),
        "lookback_days": diagnostics.get("lookback_days"),
        "empty_reason": diagnostics.get("empty_reason") or preview.get("empty_reason"),
        "recommended_command": command if signal_rows > 0 else None,
        "operator_action": operator_action,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "action_refresh_authority": "review_only_action_queue_visibility",
            "full_advisory_still_authoritative": True,
            "decision_use": "preview whether candidate-helpful causal event memory can create review-only Action Queue rows before a long advisory rerun",
        },
    }


def load_causal_memory_refresh_preview(*, asof_date: pd.Timestamp | None = None, limit: int = 25) -> dict[str, Any]:
    try:
        from advisory.signal_refresh import refresh_from_causal_memory

        raw_preview = refresh_from_causal_memory(asof_date=asof_date, limit=limit, dry_run=True)
        raw_preview["requested_asof_date"] = asof_date
        return summarize_causal_memory_refresh_preview(
            raw_preview
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_causal_memory_refresh_preview_failed",
            source="causal_memory_signal_refresh_preview",
            severity="warn",
            reason="Recommendation diagnostics could not build the read-only causal-memory signal-refresh preview.",
            error=exc,
            metadata={"asof_date": None if asof_date is None else str(asof_date), "limit": int(limit)},
        )
        return summarize_causal_memory_refresh_preview(
            {
                "target_rows": 0,
                "signal_rows": 0,
                "diagnostics": {"empty_reason": f"{exc.__class__.__name__}: {str(exc)[:200]}"},
            }
        )


def _context_reliability_commands(status: str) -> list[dict[str, str]]:
    if status in {"ok_fresh", "not_loaded"}:
        return []
    return [
        {
            "command": "./all_ml.sh --run-signal-quality-window-runner --include-signal-quality-split-reports",
            "purpose": "Refresh research-only signal-quality, context-watch, negative-pressure, and context-overlay reliability evidence.",
        },
        {
            "command": "python -m advisory.context_overlay_reliability_report --horizons 5 10 20 --format text",
            "purpose": "Inspect which news/announcement/bhavcopy/macro/exchange context families are helpful, protective, stale, or suppressed.",
        },
    ]


def summarize_context_overlay_reliability(report: dict[str, Any] | None, *, max_age_days: int = CONTEXT_RELIABILITY_MAX_AGE_DAYS) -> dict[str, Any]:
    if not isinstance(report, dict):
        return {
            "status": "not_loaded",
            "operator_action": "Context-overlay reliability evidence was not loaded for this in-memory diagnostic call.",
            "recommended_commands": [],
            "policy_boundary": {
                "report_mode": "read_only_diagnostic",
                "authority": "research_only",
                "portfolio_authority": "none",
                "broker_execution_allowed": False,
                "decision_use": "show whether context-overlay families have enough outcome evidence before runtime watch/de-risk use",
            },
        }
    if "runtime_usable_family_count" in report and isinstance(report.get("policy_boundary"), dict):
        return report
    raw_status = str(report.get("status") or "unknown").strip().lower()
    families = report.get("families") if isinstance(report.get("families"), list) else []
    class_counts = Counter(str(row.get("classification") or "unknown").strip().lower() or "unknown" for row in families if isinstance(row, dict))
    evaluated_at = pd.to_datetime(report.get("evaluated_at"), utc=True, errors="coerce")
    age_days = None
    stale = False
    if not pd.isna(evaluated_at):
        age_days = max(0.0, float((pd.Timestamp.utcnow() - evaluated_at).total_seconds()) / 86400.0)
        stale = age_days > float(max_age_days)
    status = raw_status
    if raw_status == "ok":
        status = "stale" if stale else "ok_fresh"
    elif raw_status in {"no_data", "evidence_unavailable"}:
        status = raw_status
    helpful = int(class_counts.get("candidate_helpful", 0))
    protective = int(class_counts.get("protective_candidate", 0))
    suppressed = int(
        class_counts.get("hurts_or_no_lift", 0)
        + class_counts.get("negative_after_cost", 0)
        + class_counts.get("needs_benchmark_attribution", 0)
        + class_counts.get("benchmark_beta_not_overlay_alpha", 0)
        + class_counts.get("inconsistent_or_horizon_sensitive", 0)
    )
    recommended_commands = _context_reliability_commands(status)
    if status == "ok_fresh":
        operator_action = "Use helpful/protective context families only inside their research-only runtime contracts; keep technical/risk/lifecycle confirmation mandatory."
    elif status == "stale":
        operator_action = "Refresh context-overlay reliability before trusting context family influence; stale evidence should remain annotation-only."
    elif status in {"no_data", "evidence_unavailable"}:
        operator_action = "Run research-only signal-quality/context-overlay evaluators before allowing context families to affect watch/de-risk priority."
    else:
        operator_action = "Inspect context-overlay reliability evidence before changing context influence rules."
    return {
        "status": status,
        "raw_status": raw_status,
        "evaluated_at": None if pd.isna(evaluated_at) else evaluated_at.isoformat(),
        "age_days": age_days,
        "max_age_days": int(max_age_days),
        "family_count": int(len(families)),
        "classification_counts": dict(class_counts.most_common()),
        "candidate_helpful_count": helpful,
        "protective_candidate_count": protective,
        "suppressed_or_split_required_count": suppressed,
        "runtime_usable_family_count": int(helpful + protective),
        "best_family": report.get("best_family") if isinstance(report.get("best_family"), dict) else None,
        "recommended_commands": recommended_commands,
        "operator_action": operator_action,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "authority": "research_only",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "decision_use": "show whether context-overlay families have enough outcome evidence before runtime watch/de-risk use",
        },
    }


def load_context_overlay_reliability_for_diagnostics(*, asof_date: pd.Timestamp | None = None) -> dict[str, Any]:
    try:
        summary = summarize_context_overlay_reliability(load_persisted_reliability_report(asof_date=asof_date))
        if asof_date is not None and summary.get("status") in {"no_data", "evidence_unavailable"}:
            latest = summarize_context_overlay_reliability(load_persisted_reliability_report())
            if latest.get("status") not in {"no_data", "evidence_unavailable", "not_loaded"}:
                summary["status"] = "no_point_in_time_evidence_latest_available"
                summary["latest_available_after_asof"] = latest
                summary["recommended_commands"] = latest.get("recommended_commands", [])
                summary["operator_action"] = (
                    "No point-in-time context-overlay reliability existed before the advisory as-of date, "
                    "but newer research evidence is available; do not use newer evidence to judge older decisions."
                )
        return summary
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_context_reliability_load_failed",
            source="advisory_context_overlay_reliability_summary",
            severity="warn",
            reason="Recommendation diagnostics could not load persisted context-overlay reliability evidence.",
            error=exc,
            metadata={"asof_date": None if asof_date is None else str(asof_date)},
        )
        return summarize_context_overlay_reliability({"status": "error", "families": [], "evaluated_at": None})


def summarize_signal_quality_attribution(rows: pd.DataFrame | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(rows, dict) and isinstance(rows.get("policy_boundary"), dict):
        return rows
    if not isinstance(rows, pd.DataFrame):
        return {
            "status": "not_loaded",
            "row_count": 0,
            "benchmark_beta_not_overlay_alpha_count": 0,
            "needs_benchmark_attribution_count": 0,
            "benchmark_or_attribution_blocked_count": 0,
            "operator_action": "Signal-quality attribution evidence was not loaded for this in-memory diagnostic call.",
            "recommended_commands": [],
            "policy_boundary": {
                "report_mode": "read_only_diagnostic",
                "authority": "research_only",
                "portfolio_authority": "none",
                "broker_execution_allowed": False,
                "policy_auto_promotion_allowed": False,
                "decision_use": "show whether context-overlay signal quality is blocked by market beta or missing benchmark attribution before blaming regime policy",
            },
        }
    if rows.empty:
        status = "no_rows"
        latest = pd.NaT
        benchmark_beta = 0
        needs_benchmark = 0
        variants: list[str] = []
    else:
        empty = pd.Series([""] * len(rows), index=rows.index, dtype=object)
        recommendations = rows.get("recommendation", empty).fillna("").astype(str).str.strip()
        classifications = rows.get("classification", empty).fillna("").astype(str).str.strip()
        benchmark_beta = int(
            (recommendations.eq("benchmark_beta_not_overlay_alpha") | classifications.eq("benchmark_beta_not_overlay_alpha")).sum()
        )
        needs_benchmark = int(
            (recommendations.eq("needs_benchmark_attribution") | classifications.eq("needs_benchmark_attribution")).sum()
        )
        latest = pd.to_datetime(rows.get("evaluated_at", pd.Series(dtype=object)), utc=True, errors="coerce").max()
        blocked_mask = (
            recommendations.isin(["benchmark_beta_not_overlay_alpha", "needs_benchmark_attribution"])
            | classifications.isin(["benchmark_beta_not_overlay_alpha", "needs_benchmark_attribution"])
        )
        variants = sorted(str(value) for value in rows.loc[blocked_mask, "variant"].dropna().unique())[:10] if "variant" in rows.columns else []
        status = "benchmark_or_attribution_blocked" if benchmark_beta + needs_benchmark > 0 else "ok_no_attribution_blockers"
    blocked = int(benchmark_beta + needs_benchmark)
    commands = [
        {
            "command": "python -m advisory.signal_quality_window_runner --horizons 5 10 20 --include-split-reports",
            "purpose": "Run rolling-window and split diagnostics before treating raw positive context evidence as useful.",
        },
        {
            "command": "python -m advisory.context_overlay_reliability_report --horizons 5 10 20 --format text",
            "purpose": "Inspect fast context-overlay family/class reliability and benchmark attribution.",
        },
    ]
    return {
        "status": status,
        "evaluated_at": None if pd.isna(latest) else latest.isoformat(),
        "row_count": int(len(rows)),
        "benchmark_beta_not_overlay_alpha_count": benchmark_beta,
        "needs_benchmark_attribution_count": needs_benchmark,
        "benchmark_or_attribution_blocked_count": blocked,
        "blocked_variants": variants,
        "operator_action": (
            "Treat context signal-quality evidence as research-only until benchmark-beta and missing-attribution rows are narrowed or resolved."
            if blocked
            else "No benchmark-beta or missing-attribution blockers were found in the loaded signal-quality summary rows."
        ),
        "recommended_commands": commands if blocked else [],
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "authority": "research_only",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "decision_use": "attribute missing BUYs across benchmark-attributed context evidence before changing global regime policy",
        },
    }


def load_signal_quality_attribution_for_diagnostics(*, asof_date: pd.Timestamp | None = None, limit: int = 250) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": int(limit)}
    where = ""
    if asof_date is not None:
        where = "WHERE evaluated_at <= %(asof_date)s"
        params["asof_date"] = pd.to_datetime(asof_date, utc=True, errors="coerce")
    try:
        latest_df = sql_to_df(
            f"SELECT MAX(evaluated_at) AS latest_evaluated_at FROM {SIGNAL_QUALITY_SUMMARY_TABLE} {where}",
            params=params,
            retries=3,
            statement_timeout_ms=10000,
        )
        latest = pd.to_datetime(latest_df.iloc[0].get("latest_evaluated_at"), utc=True, errors="coerce") if not latest_df.empty else pd.NaT
        if pd.isna(latest):
            return summarize_signal_quality_attribution(pd.DataFrame())
        rows = sql_to_df(
            f"""
            SELECT *
            FROM {SIGNAL_QUALITY_SUMMARY_TABLE}
            WHERE evaluated_at = %(latest)s
            ORDER BY horizon_days, variant
            LIMIT %(limit)s
            """,
            params={"latest": latest, "limit": int(limit)},
            retries=3,
            statement_timeout_ms=10000,
        )
        return summarize_signal_quality_attribution(rows)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_signal_quality_attribution_load_failed",
            source=SIGNAL_QUALITY_SUMMARY_TABLE,
            severity="warn",
            reason="Recommendation diagnostics could not load latest signal-quality attribution evidence.",
            error=exc,
            metadata={"asof_date": None if asof_date is None else str(asof_date), "limit": int(limit)},
        )
        return summarize_signal_quality_attribution(pd.DataFrame())


def summarize_market_context_blocks(row_diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    dimensions = ["breadth", "macro_stress", "sector_symbol", "technical", "context_overlay"]
    reason_counts: Counter[str] = Counter()
    macro_state_counts: Counter[str] = Counter()
    blocked_label_counts: dict[str, Counter[str]] = {dimension: Counter() for dimension in dimensions}
    samples: list[dict[str, Any]] = []
    for item in row_diagnostics:
        market = item.get("market_context") if isinstance(item.get("market_context"), dict) else {}
        if market.get("adjustment") != "positive_action_blocked_by_market_context":
            continue
        block_reason = str(market.get("block_reason") or "").strip() or "unknown"
        reason_counts[block_reason] += 1
        macro_state = str(market.get("macro_risk_state") or "").strip().upper() or "missing"
        macro_state_counts[macro_state] += 1
        labels = item.get("multi_context_labels") if isinstance(item.get("multi_context_labels"), dict) else {}
        for dimension in dimensions:
            label = str(labels.get(dimension) or "missing").strip() or "missing"
            blocked_label_counts[dimension][label] += 1
        if len(samples) < 25:
            samples.append(
                {
                    "symbol": item.get("symbol"),
                    "action_code": item.get("action_code"),
                    "action_source": item.get("action_source"),
                    "block_reason": block_reason,
                    "block_reason_inferred": bool(market.get("block_reason_inferred")),
                    "macro_risk_state": market.get("macro_risk_state"),
                    "regime_name": market.get("regime_name"),
                    "risk_off_score": market.get("risk_off_score"),
                    "breadth_trend_alignment_pct": market.get("breadth_trend_alignment_pct"),
                    "macro_hard_risk": bool(market.get("macro_hard_risk")),
                    "score_risk_off_hard_block": bool(market.get("score_risk_off_hard_block")),
                    "weak_breadth_block": bool(market.get("weak_breadth_block")),
                    "multi_context_labels": {dimension: labels.get(dimension) for dimension in dimensions if labels.get(dimension)},
                    "reason": market.get("reason"),
                }
            )
    total = int(sum(reason_counts.values()))
    return {
        "status": "market_context_blocks_present" if total else "no_market_context_blocks",
        "blocked_count": total,
        "reason_counts": dict(reason_counts.most_common()),
        "macro_risk_state_counts": dict(macro_state_counts.most_common()),
        "blocked_label_counts": {dimension: dict(counter.most_common()) for dimension, counter in blocked_label_counts.items()},
        "samples": samples,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "separate hard macro-risk blocks from score-only, weak-breadth, or regime-label context gates",
        },
    }


def summarize_multi_context_attribution(row_diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    dimensions = ["breadth", "macro_stress", "sector_symbol", "technical", "context_overlay"]
    label_counts: dict[str, Counter[str]] = {dimension: Counter() for dimension in dimensions}
    suppressed_dimension_counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    for item in row_diagnostics:
        labels = item.get("multi_context_labels") if isinstance(item.get("multi_context_labels"), dict) else {}
        non_neutral: dict[str, str] = {}
        for dimension in dimensions:
            label = str(labels.get(dimension) or "missing").strip() or "missing"
            label_counts[dimension][label] += 1
            if label in {
                "weak_breadth",
                "hard_macro_risk",
                "risk_off_score_high",
                "not_in_top_context_universe",
                "ranked_no_leadership",
                "weak_or_unconfirmed",
                "adverse_or_exit",
                "unknown",
                "conflicts_with_positive_action",
                "context_conflicts_with_selected_action",
                "mixed_or_conflicting_context",
            }:
                suppressed_dimension_counts[dimension] += 1
                non_neutral[dimension] = label
        if non_neutral and len(samples) < 25:
            samples.append(
                {
                    "symbol": item.get("symbol"),
                    "action_code": item.get("action_code"),
                    "action_source": item.get("action_source"),
                    "labels": non_neutral,
                    "suppression_reasons": item.get("suppression_reasons") or [],
                }
            )
    row_count = len(row_diagnostics)
    active_dimensions = [dimension for dimension, count in suppressed_dimension_counts.items() if int(count) > 0]
    missing_label_counts = {
        dimension: int(counter.get("missing") or 0)
        for dimension, counter in label_counts.items()
        if int(counter.get("missing") or 0) > 0
    }
    missing_label_total = int(sum(missing_label_counts.values()))
    recommended_commands: list[dict[str, str]] = []
    if not row_count:
        status = "no_action_rows"
        operator_action = "Run advisory/action refresh before interpreting market context; no action rows exist to attribute."
    elif active_dimensions:
        status = "multi_context_attribution_available"
        operator_action = (
            "Read breadth, macro, sector/symbol, technical, and context-overlay labels separately before changing any global regime policy."
        )
        if missing_label_total:
            operator_action += " Some persisted rows still lack full labels; preview reason-contract repair to backfill explanation metadata."
    else:
        status = "multi_context_no_adverse_labels"
        operator_action = "No adverse multi-context labels were found in current action rows; look at candidate generation or technical confirmation next."
        if missing_label_total:
            operator_action += " Some persisted rows still lack full labels; preview reason-contract repair before drawing conclusions from missing context fields."
    if row_count and missing_label_total:
        recommended_commands.append(
            {
                "command": "python -m advisory.action_recommender --repair-reason-contracts --date YYYY-MM-DD --dry-run --format text",
                "purpose": "Preview metadata-only backfill for missing multi-context labels on persisted action rows.",
                "authority": "metadata_repair_preview_no_action_portfolio_or_broker_change",
            }
        )
    return {
        "status": status,
        "row_count": int(row_count),
        "label_counts": {dimension: dict(counter.most_common()) for dimension, counter in label_counts.items()},
        "suppressed_dimension_counts": dict(suppressed_dimension_counts.most_common()),
        "missing_label_counts": missing_label_counts,
        "missing_label_total": missing_label_total,
        "active_dimensions": active_dimensions,
        "samples": samples,
        "recommended_commands": recommended_commands,
        "operator_action": operator_action,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "separate layered market/news/macro/technical context from any single global regime label",
        },
    }


def summarize_research_backed_context_policy_alignment(
    *,
    context_gate_policy: dict[str, Any] | None = None,
    multi_context_attribution: dict[str, Any] | None = None,
    context_overlay_reliability: dict[str, Any] | None = None,
    signal_quality_attribution: dict[str, Any] | None = None,
    reviewed_context_rule_eligibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context_gate_policy = context_gate_policy if isinstance(context_gate_policy, dict) else {}
    multi_context_attribution = multi_context_attribution if isinstance(multi_context_attribution, dict) else {}
    context_overlay_reliability = (
        context_overlay_reliability
        if isinstance(context_overlay_reliability, dict) and isinstance(context_overlay_reliability.get("policy_boundary"), dict)
        else summarize_context_overlay_reliability(context_overlay_reliability)
    )
    signal_quality_attribution = summarize_signal_quality_attribution(signal_quality_attribution)
    reviewed_context_rule_eligibility = reviewed_context_rule_eligibility if isinstance(reviewed_context_rule_eligibility, dict) else {}
    exposure = (
        context_gate_policy.get("single_regime_hard_gate_exposure")
        if isinstance(context_gate_policy.get("single_regime_hard_gate_exposure"), dict)
        else {}
    )
    active_single_regime_flags = list(exposure.get("active_single_regime_flags") or [])
    active_context_hard_flags = list(exposure.get("active_context_hard_flags") or [])
    active_dimensions = list(multi_context_attribution.get("active_dimensions") or [])
    missing_label_total = int(multi_context_attribution.get("missing_label_total") or 0)
    runtime_usable_families = int(context_overlay_reliability.get("runtime_usable_family_count") or 0)
    attribution_blockers = int(signal_quality_attribution.get("benchmark_or_attribution_blocked_count") or 0)
    eligible_rules = int(reviewed_context_rule_eligibility.get("eligible_rule_count") or 0)
    ineligible_rules = int(reviewed_context_rule_eligibility.get("ineligible_rule_count") or 0)
    total_rules = int(reviewed_context_rule_eligibility.get("total_rule_count") or 0)
    blockers: list[str] = []
    if active_single_regime_flags:
        blockers.append("single_regime_hard_gate_enabled")
    if attribution_blockers > 0:
        blockers.append("benchmark_or_attribution_blocked_context_evidence")
    if missing_label_total > 0:
        blockers.append("missing_multi_context_labels")
    if total_rules > 0 and eligible_rules == 0:
        blockers.append("reviewed_context_rules_annotation_only")
    if active_single_regime_flags:
        status = "single_regime_policy_exposed"
        operator_action = (
            "A broad regime-label hard gate is active. Do not treat missing BUYs as validated context policy until that gate is disabled or explicitly justified."
        )
    elif attribution_blockers > 0:
        status = "layered_context_attribution_blocked"
        operator_action = (
            "Layered context is visible, but some context evidence is benchmark-beta-only or lacks attribution; run split/benchmark diagnostics before policy changes."
        )
    elif missing_label_total > 0:
        status = "layered_context_metadata_incomplete"
        operator_action = (
            "Layered context policy is mostly diagnostic, but persisted rows lack some breadth/macro/sector/technical/context labels; run metadata repair before drawing conclusions."
        )
    elif runtime_usable_families > 0 or eligible_rules > 0 or active_dimensions:
        status = "layered_context_policy_aligned"
        operator_action = (
            "Diagnostics are using layered context dimensions and benchmark-gated context families; inspect technical confirmation and downstream consolidation before changing global regime policy."
        )
    else:
        status = "layered_context_not_evidenced"
        operator_action = (
            "No active single-regime hard gate was found, but this diagnostic lacks enough labelled context or reliability evidence; collect/refresh context evidence before policy tuning."
        )
    return {
        "status": status,
        "blockers": blockers,
        "active_single_regime_flags": active_single_regime_flags,
        "active_context_hard_flags": active_context_hard_flags,
        "active_multi_context_dimensions": active_dimensions,
        "missing_multi_context_label_total": missing_label_total,
        "runtime_usable_context_family_count": runtime_usable_families,
        "signal_quality_attribution_blocked_count": attribution_blockers,
        "reviewed_context_rule_count": total_rules,
        "runtime_eligible_reviewed_context_rule_count": eligible_rules,
        "runtime_ineligible_reviewed_context_rule_count": ineligible_rules,
        "research_basis": [
            "Do not treat raw positive context returns as alpha without benchmark attribution and costs.",
            "Separate market, macro, sector/symbol, event-overlay, and technical dimensions instead of relying on one global regime label.",
            "Keep LLM/news/macro context as structured evidence or bounded review-only policy until realised outcome evidence supports runtime use.",
        ],
        "operator_action": operator_action,
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "authority": "research_policy_alignment_only",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "decision_use": "audit whether missing recommendations should be diagnosed through layered context evidence instead of a single global regime label",
        },
    }


def summarize_recommendation_rows(
    rows: pd.DataFrame,
    *,
    asof_date: pd.Timestamp | None = None,
    candidates: pd.DataFrame | None = None,
    rejections: pd.DataFrame | None = None,
    allocations: pd.DataFrame | None = None,
    portfolio: pd.DataFrame | None = None,
    candidate_source_availability: dict[str, Any] | None = None,
    post_advisory_activity: dict[str, Any] | None = None,
    context_overlay_reliability: dict[str, Any] | None = None,
    signal_quality_attribution: dict[str, Any] | None = None,
    context_overlay_source_diagnostics: dict[str, Any] | None = None,
    context_watchlist_intake: dict[str, Any] | None = None,
    context_watch_technical_precheck: dict[str, Any] | None = None,
    context_overlay_refresh_preview: dict[str, Any] | None = None,
    context_overlay_signal_refresh_state: dict[str, Any] | None = None,
    causal_memory_refresh_preview: dict[str, Any] | None = None,
    market_participation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context_watch_technical_precheck = (
        context_watch_technical_precheck
        if isinstance(context_watch_technical_precheck, dict)
        else {}
    )
    upstream = summarize_upstream_candidates(
        candidates if isinstance(candidates, pd.DataFrame) else pd.DataFrame(),
        rejections if isinstance(rejections, pd.DataFrame) else pd.DataFrame(),
        rows,
    )
    candidate_source_availability = candidate_source_availability if isinstance(candidate_source_availability, dict) else {}
    handoff = summarize_risk_portfolio_handoff(allocations, portfolio)
    context_gate_policy = current_context_gate_policy_snapshot()
    code_data_freshness = summarize_code_data_freshness(
        rows=rows,
        candidates=candidates,
        rejections=rejections,
        allocations=allocations,
        portfolio=portfolio,
    )
    if not isinstance(post_advisory_activity, dict):
        post_advisory_activity = {
            "status": "not_loaded",
            "total_new_rows": 0,
            "full_advisory_required_count": 0,
            "action_refresh_candidate_count": 0,
            "action_refresh_candidate_counts": {},
            "action_refresh_recommended": False,
            "action_refresh_command": None,
            "full_advisory_rerun_recommended": False,
            "operator_action": "Post-advisory watcher activity was not loaded for this in-memory diagnostic summary.",
        }
    diagnostic_trust = summarize_diagnostic_trust(
        code_data_freshness=code_data_freshness,
        upstream=upstream,
        context_gate_policy=context_gate_policy,
    )
    technical_artifacts = summarize_technical_artifacts(
        upstream=upstream,
        code_data_freshness=code_data_freshness,
    )
    stale_context_gate_artifacts = summarize_stale_context_gate_artifacts(
        upstream=upstream,
        context_gate_policy=context_gate_policy,
        code_data_freshness=code_data_freshness,
    )
    context_overlay_reliability = summarize_context_overlay_reliability(context_overlay_reliability)
    signal_quality_attribution = summarize_signal_quality_attribution(signal_quality_attribution)
    context_overlay_source_diagnostics = summarize_context_overlay_source_availability(context_overlay_source_diagnostics)
    context_watchlist_intake = (
        context_watchlist_intake
        if isinstance(context_watchlist_intake, dict)
        else summarize_context_watchlist_intake(None)
    )
    context_overlay_refresh_preview = summarize_context_overlay_refresh_preview(context_overlay_refresh_preview)
    context_overlay_signal_refresh_state = summarize_context_overlay_signal_refresh_state(
        context_overlay_signal_refresh_state,
        context_overlay_refresh_preview=context_overlay_refresh_preview,
    )
    causal_memory_refresh_preview = summarize_causal_memory_refresh_preview(causal_memory_refresh_preview)
    market_participation_context = summarize_market_participation_context(market_participation_context)
    if rows.empty:
        manual_review_breakdown = summarize_manual_review_breakdown([])
        multi_context_attribution = summarize_multi_context_attribution([])
        advisory_rerun_readiness = summarize_advisory_rerun_readiness(
            candidate_source_availability=candidate_source_availability,
            suppression_counts={},
            context_overlay_source_diagnostics=context_overlay_source_diagnostics,
            context_watchlist_intake=context_watchlist_intake,
            context_overlay_refresh_preview=context_overlay_refresh_preview,
            context_overlay_signal_refresh_state=context_overlay_signal_refresh_state,
            causal_memory_refresh_preview=causal_memory_refresh_preview,
            post_advisory_activity=post_advisory_activity,
        )
        context_overlay_refresh_preview = align_context_overlay_preview_to_readiness(
            context_overlay_refresh_preview,
            advisory_rerun_readiness,
        )
        post_advisory_activity = align_post_advisory_activity_to_readiness(
            post_advisory_activity,
            advisory_rerun_readiness,
        )
        primary_no_buy_cause = classify_no_data_primary_cause(
            asof_date=asof_date,
            diagnostic_trust=diagnostic_trust,
            candidate_source_availability=candidate_source_availability,
        )
        primary_no_buy_cause = apply_advisory_rerun_readiness_to_primary_cause(
            primary_no_buy_cause,
            advisory_rerun_readiness,
        )
        action_queue_authority_state = summarize_action_queue_authority_state(
            row_count=0,
            action_source_counts={},
            candidate_source_availability=candidate_source_availability,
            broker_candidate_count=0,
        )
        context_to_entry_pipeline = summarize_context_to_entry_pipeline(
            context_overlay_pressure={},
            upstream=upstream,
            action_counts={},
            context_overlay_source_diagnostics=context_overlay_source_diagnostics,
            context_watchlist_intake=context_watchlist_intake,
            context_watch_technical_precheck=context_watch_technical_precheck,
            context_overlay_refresh_preview=context_overlay_refresh_preview,
            market_participation_context=market_participation_context,
        )
        primary_no_buy_cause = apply_context_to_entry_commands_to_primary_cause(
            primary_no_buy_cause,
            context_to_entry_pipeline,
        )
        action_queue_authority_state = align_action_queue_authority_to_primary_cause(
            action_queue_authority_state,
            primary_no_buy_cause,
        )
        advisory_rerun_readiness = align_advisory_rerun_readiness_to_primary_cause(
            advisory_rerun_readiness,
            primary_no_buy_cause,
        )
        layered_underparticipation_attribution = summarize_layered_underparticipation_attribution(
            positive_count=0,
            action_counts={},
            upstream=upstream,
            candidate_source_availability=candidate_source_availability,
            context_overlay_pressure={},
            context_overlay_source_diagnostics=context_overlay_source_diagnostics,
            context_overlay_refresh_preview=context_overlay_refresh_preview,
            context_overlay_signal_refresh_state=context_overlay_signal_refresh_state,
            signal_quality_attribution=signal_quality_attribution,
            market_context_blocks={},
            market_participation_context=market_participation_context,
            primary_no_buy_cause=primary_no_buy_cause,
        )
        trusted_context_rule_adjustments = summarize_trusted_context_rule_adjustments([])
        reviewed_context_rule_eligibility = summarize_reviewed_context_rule_eligibility([])
        research_context_policy_alignment = summarize_research_backed_context_policy_alignment(
            context_gate_policy=context_gate_policy,
            multi_context_attribution=multi_context_attribution,
            context_overlay_reliability=context_overlay_reliability,
            signal_quality_attribution=signal_quality_attribution,
            reviewed_context_rule_eligibility=reviewed_context_rule_eligibility,
        )
        return {
            "status": "no_data",
            "asof_date": None if asof_date is None else asof_date.isoformat(),
            "row_count": 0,
            "action_counts": {},
            "action_source_counts": {},
            "action_queue_authority_state": action_queue_authority_state,
            "positive_action_count": 0,
            "broker_candidate_count": 0,
            "suppression_counts": {},
            "upstream_candidates": upstream,
            "candidate_source_availability": candidate_source_availability,
            "risk_portfolio_handoff": handoff,
            "context_gate_policy": context_gate_policy,
            "code_data_freshness": code_data_freshness,
            "post_advisory_activity": post_advisory_activity,
            "technical_artifacts": technical_artifacts,
            "stale_context_gate_artifacts": stale_context_gate_artifacts,
            "context_overlay_reliability": context_overlay_reliability,
            "signal_quality_attribution": signal_quality_attribution,
            "context_overlay_source_diagnostics": context_overlay_source_diagnostics,
            "context_watchlist_intake": context_watchlist_intake,
            "context_watch_technical_precheck": context_watch_technical_precheck,
            "context_overlay_refresh_preview": context_overlay_refresh_preview,
            "context_overlay_signal_refresh_state": context_overlay_signal_refresh_state,
            "causal_memory_refresh_preview": causal_memory_refresh_preview,
            "market_participation_context": market_participation_context,
            "multi_context_attribution": multi_context_attribution,
            "research_context_policy_alignment": research_context_policy_alignment,
            "manual_review_breakdown": manual_review_breakdown,
            "trusted_context_rule_adjustments": trusted_context_rule_adjustments,
            "reviewed_context_rule_eligibility": reviewed_context_rule_eligibility,
            "advisory_rerun_readiness": advisory_rerun_readiness,
            "layered_underparticipation_attribution": layered_underparticipation_attribution,
            "context_to_entry_pipeline": context_to_entry_pipeline,
            "diagnostic_trust": diagnostic_trust,
            "no_buy_diagnosis": "no_persisted_action_recommendation_rows",
            "primary_no_buy_cause": primary_no_buy_cause,
            "operator_next_steps": _no_data_operator_next_steps(primary_no_buy_cause),
            "policy_boundary": {
                "report_mode": "read_only_diagnostic",
                "portfolio_authority": "none",
                "broker_execution_allowed": False,
                "decision_use": "debug why recommendations are missing; do not trade from this report alone",
            },
            "diagnosis": "No persisted action recommendations were found for the requested date.",
        }
    records = rows.to_dict(orient="records")
    row_diagnostics = [_row_diagnostics(row) for row in records]
    action_counts = Counter(item["action_code"] for item in row_diagnostics)
    source_counts = Counter(item.get("action_source") or "unknown" for item in row_diagnostics)
    suppression_counts: Counter[str] = Counter()
    context_label_counts: dict[str, Counter[str]] = {
        "breadth": Counter(),
        "macro_stress": Counter(),
        "sector_symbol": Counter(),
        "technical": Counter(),
        "context_overlay": Counter(),
    }
    for item in row_diagnostics:
        for reason in item["suppression_reasons"]:
            _count(suppression_counts, reason)
        for key, value in item.get("multi_context_labels", {}).items():
            _count(context_label_counts.setdefault(key, Counter()), str(value or "missing"))
    context_overlay_pressure = summarize_context_overlay_pressure(row_diagnostics)
    market_context_blocks = summarize_market_context_blocks(row_diagnostics)
    manual_review_breakdown = summarize_manual_review_breakdown(row_diagnostics)
    transition_policy_gates = summarize_transition_policy_gates(row_diagnostics)
    trusted_context_rule_adjustments = summarize_trusted_context_rule_adjustments(row_diagnostics)
    reviewed_context_rule_eligibility = summarize_reviewed_context_rule_eligibility(row_diagnostics)
    multi_context_attribution = summarize_multi_context_attribution(row_diagnostics)
    advisory_rerun_readiness = summarize_advisory_rerun_readiness(
        candidate_source_availability=candidate_source_availability,
        suppression_counts=suppression_counts,
        context_overlay_source_diagnostics=context_overlay_source_diagnostics,
        context_watchlist_intake=context_watchlist_intake,
        context_overlay_refresh_preview=context_overlay_refresh_preview,
        context_overlay_signal_refresh_state=context_overlay_signal_refresh_state,
        causal_memory_refresh_preview=causal_memory_refresh_preview,
        post_advisory_activity=post_advisory_activity,
    )
    context_overlay_refresh_preview = align_context_overlay_preview_to_readiness(
        context_overlay_refresh_preview,
        advisory_rerun_readiness,
    )
    post_advisory_activity = align_post_advisory_activity_to_readiness(
        post_advisory_activity,
        advisory_rerun_readiness,
    )

    positive_rows = [item for item in row_diagnostics if item["action_code"] in POSITIVE_ACTIONS]
    broker_rows = [
        item
        for item in row_diagnostics
        if item["action_code"] in BROKER_ACTIONS
        and str(item.get("execution_mode") or "").lower() == "broker_order"
        and str(item.get("reason_contract_status") or "").lower() == "complete"
    ]
    action_queue_authority_state = summarize_action_queue_authority_state(
        row_count=len(row_diagnostics),
        action_source_counts=source_counts,
        candidate_source_availability=candidate_source_availability,
        broker_candidate_count=len(broker_rows),
    )
    blocked_or_review_rows = [
        item
        for item in row_diagnostics
        if item["action_code"] in {"MANUAL_REVIEW", "WATCH", "HOLD"}
        or item.get("suppression_reasons")
    ]
    blocked_or_review_rows.sort(key=lambda item: (len(item.get("suppression_reasons") or []), item.get("invest_score_pct") or -1), reverse=True)
    no_buy_diagnosis = "positive_recommendations_available" if positive_rows else "no_final_buy_or_buy_more_rows"
    if (
        not positive_rows
        and int(transition_policy_gates.get("transition_policy_gate_count") or 0) > 0
        and transition_gates_include_entry_actions(transition_policy_gates)
    ):
        no_buy_diagnosis = "transition_stability_policy_gate_downgraded_broker_actions"
    elif not positive_rows and suppression_counts:
        no_buy_diagnosis = "no_positive_rows_with_visible_suppression_reasons"
    elif (
        not positive_rows
        and (
            int(handoff.get("risk_base_unconfirmed_count") or 0) > 0
            or int(handoff.get("portfolio_technical_entry_not_confirmed_count") or 0) > 0
        )
    ):
        no_buy_diagnosis = "risk_or_portfolio_blocked_unconfirmed_technical_entries"
    elif not positive_rows and int(upstream.get("confirmed_entry_lost_downstream_count") or 0) > 0:
        no_buy_diagnosis = "confirmed_technical_candidates_lost_downstream"
    elif not positive_rows and int(upstream.get("positive_or_constructive_candidate_count") or 0) > 0:
        no_buy_diagnosis = "constructive_upstream_candidates_did_not_survive_action_consolidation"
    elif not positive_rows and int(context_overlay_pressure.get("positive_or_watch_pressure_count") or 0) > 0:
        no_buy_diagnosis = "context_overlay_watch_pressure_waiting_for_confirmation"
    elif not positive_rows and _exit_only_action_mix(action_counts):
        no_buy_diagnosis = "exit_only_action_mix_no_entry_candidates"
    elif not positive_rows and action_counts.get("WATCH", 0):
        no_buy_diagnosis = "watch_only_no_final_buy_rows"
    primary_no_buy_cause = classify_primary_no_buy_cause(
        positive_count=len(positive_rows),
        suppression_counts=suppression_counts,
        upstream=upstream,
        handoff=handoff,
        diagnostic_trust=diagnostic_trust,
        action_counts=action_counts,
        context_overlay_pressure=context_overlay_pressure,
        post_advisory_activity=post_advisory_activity,
        market_context_blocks=market_context_blocks,
        technical_artifacts=technical_artifacts,
        context_overlay_refresh_preview=context_overlay_refresh_preview,
        context_overlay_signal_refresh_state=context_overlay_signal_refresh_state,
        causal_memory_refresh_preview=causal_memory_refresh_preview,
        market_participation_context=market_participation_context,
        candidate_source_availability=candidate_source_availability,
        transition_policy_gates=transition_policy_gates,
    )
    primary_no_buy_cause = apply_advisory_rerun_readiness_to_primary_cause(
        primary_no_buy_cause,
        advisory_rerun_readiness,
    )
    context_to_entry_pipeline = summarize_context_to_entry_pipeline(
        context_overlay_pressure=context_overlay_pressure,
        upstream=upstream,
        action_counts=action_counts,
        context_overlay_source_diagnostics=context_overlay_source_diagnostics,
        context_watchlist_intake=context_watchlist_intake,
        context_watch_technical_precheck=context_watch_technical_precheck,
        context_overlay_refresh_preview=context_overlay_refresh_preview,
        market_participation_context=market_participation_context,
    )
    primary_no_buy_cause = apply_context_to_entry_commands_to_primary_cause(
        primary_no_buy_cause,
        context_to_entry_pipeline,
    )
    action_queue_authority_state = align_action_queue_authority_to_primary_cause(
        action_queue_authority_state,
        primary_no_buy_cause,
    )
    advisory_rerun_readiness = align_advisory_rerun_readiness_to_primary_cause(
        advisory_rerun_readiness,
        primary_no_buy_cause,
    )
    layered_underparticipation_attribution = summarize_layered_underparticipation_attribution(
        positive_count=len(positive_rows),
        action_counts=action_counts,
        upstream=upstream,
        candidate_source_availability=candidate_source_availability,
        context_overlay_pressure=context_overlay_pressure,
        context_overlay_source_diagnostics=context_overlay_source_diagnostics,
        context_overlay_refresh_preview=context_overlay_refresh_preview,
        context_overlay_signal_refresh_state=context_overlay_signal_refresh_state,
        signal_quality_attribution=signal_quality_attribution,
        market_context_blocks=market_context_blocks,
        market_participation_context=market_participation_context,
        primary_no_buy_cause=primary_no_buy_cause,
    )
    research_context_policy_alignment = summarize_research_backed_context_policy_alignment(
        context_gate_policy=context_gate_policy,
        multi_context_attribution=multi_context_attribution,
        context_overlay_reliability=context_overlay_reliability,
        signal_quality_attribution=signal_quality_attribution,
        reviewed_context_rule_eligibility=reviewed_context_rule_eligibility,
    )
    primary_code = str(primary_no_buy_cause.get("code") or "")
    if not positive_rows and primary_code in PRIMARY_CAUSE_DIAGNOSIS_OVERRIDES:
        no_buy_diagnosis = primary_code

    return {
        "status": "ok",
        "asof_date": pd.to_datetime(rows["asof_date"].iloc[0], utc=True, errors="coerce").isoformat(),
        "row_count": int(len(rows)),
        "action_counts": dict(sorted(action_counts.items())),
        "action_source_counts": dict(source_counts.most_common()),
        "action_queue_authority_state": action_queue_authority_state,
        "positive_action_count": int(len(positive_rows)),
        "broker_candidate_count": int(len(broker_rows)),
        "suppression_counts": dict(suppression_counts.most_common()),
        "context_label_counts": {key: dict(value.most_common()) for key, value in context_label_counts.items()},
        "context_overlay_pressure": context_overlay_pressure,
        "market_context_blocks": market_context_blocks,
        "transition_policy_gates": transition_policy_gates,
        "trusted_context_rule_adjustments": trusted_context_rule_adjustments,
        "reviewed_context_rule_eligibility": reviewed_context_rule_eligibility,
        "upstream_candidates": upstream,
        "candidate_source_availability": candidate_source_availability,
        "risk_portfolio_handoff": handoff,
        "context_gate_policy": context_gate_policy,
        "code_data_freshness": code_data_freshness,
        "post_advisory_activity": post_advisory_activity,
        "technical_artifacts": technical_artifacts,
        "stale_context_gate_artifacts": stale_context_gate_artifacts,
        "context_overlay_reliability": context_overlay_reliability,
        "signal_quality_attribution": signal_quality_attribution,
        "context_overlay_source_diagnostics": context_overlay_source_diagnostics,
        "context_watchlist_intake": context_watchlist_intake,
        "context_watch_technical_precheck": context_watch_technical_precheck,
        "context_overlay_refresh_preview": context_overlay_refresh_preview,
        "context_overlay_signal_refresh_state": context_overlay_signal_refresh_state,
        "causal_memory_refresh_preview": causal_memory_refresh_preview,
        "market_participation_context": market_participation_context,
        "multi_context_attribution": multi_context_attribution,
        "research_context_policy_alignment": research_context_policy_alignment,
        "manual_review_breakdown": manual_review_breakdown,
        "advisory_rerun_readiness": advisory_rerun_readiness,
        "layered_underparticipation_attribution": layered_underparticipation_attribution,
        "context_to_entry_pipeline": context_to_entry_pipeline,
        "diagnostic_trust": diagnostic_trust,
        "no_buy_diagnosis": no_buy_diagnosis,
        "primary_no_buy_cause": primary_no_buy_cause,
        "possible_single_regime_label_block_count": int(suppression_counts.get("possible_single_regime_label_block", 0)),
        "top_blocked_or_review_symbols": blocked_or_review_rows[:25],
        "sample_positive_symbols": positive_rows[:25],
        "operator_next_steps": _operator_next_steps(
            positive_count=len(positive_rows),
            suppression_counts=suppression_counts,
            broker_candidate_count=len(broker_rows),
            upstream=upstream,
            handoff=handoff,
            context_gate_policy=context_gate_policy,
            code_data_freshness=code_data_freshness,
            diagnostic_trust=diagnostic_trust,
            action_counts=action_counts,
            context_overlay_pressure=context_overlay_pressure,
            context_overlay_source_diagnostics=context_overlay_source_diagnostics,
            context_overlay_refresh_preview=context_overlay_refresh_preview,
            context_overlay_signal_refresh_state=context_overlay_signal_refresh_state,
            context_to_entry_pipeline=context_to_entry_pipeline,
            signal_quality_attribution=signal_quality_attribution,
            causal_memory_refresh_preview=causal_memory_refresh_preview,
            market_context_blocks=market_context_blocks,
            post_advisory_activity=post_advisory_activity,
            technical_artifacts=technical_artifacts,
            market_participation_context=market_participation_context,
            manual_review_breakdown=manual_review_breakdown,
            candidate_source_availability=candidate_source_availability,
            transition_policy_gates=transition_policy_gates,
            action_queue_authority_state=action_queue_authority_state,
            primary_no_buy_cause=primary_no_buy_cause,
        ),
        "policy_boundary": {
            "report_mode": "read_only_diagnostic",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "debug why recommendations are missing or downgraded; do not trade from this report alone",
        },
    }


def _candidate_source_primary_cause(candidate_source_availability: dict[str, Any]) -> dict[str, Any]:
    status = str(candidate_source_availability.get("status") or "").strip()
    relation = str(candidate_source_availability.get("latest_candidate_relation") or "").strip()
    requested = candidate_source_availability.get("requested_asof_date")
    latest = candidate_source_availability.get("latest_candidate_asof_date")
    evidence = {
        "candidate_source_status": status,
        "requested_asof_date": requested,
        "candidate_rows_for_asof": int(candidate_source_availability.get("candidate_rows_for_asof") or 0),
        "latest_candidate_asof_date": latest,
        "latest_candidate_rows": int(candidate_source_availability.get("latest_candidate_rows") or 0),
        "date_gap_days": candidate_source_availability.get("date_gap_days"),
        "latest_candidate_relation": relation or None,
        "latest_candidate_age_days": candidate_source_availability.get("latest_candidate_age_days"),
        "latest_candidate_trading_age_days": candidate_source_availability.get("latest_candidate_trading_age_days"),
        "policy_boundary": candidate_source_availability.get("policy_boundary", {}),
    }
    if status == "candidate_rows_missing_for_asof_latest_exists":
        code = (
            "candidate_source_stale_for_asof"
            if relation == "latest_candidate_older_than_requested"
            else "candidate_source_date_mismatch"
        )
        operator_action = (
            "Rule-engine candidate rows are missing for the diagnostic date, and the latest available candidate evidence is older. "
            "Run all_advisory.sh for the relevant trading date before interpreting missing BUYs as regime, threshold, context, or LLM behavior."
            if relation == "latest_candidate_older_than_requested"
            else "Rule-engine candidate rows are missing for the diagnostic date, although another candidate date exists. "
            "Run all_advisory.sh for the relevant trading date before interpreting missing BUYs as regime, threshold, context, or LLM behavior."
        )
    elif status == "candidate_rows_missing":
        code = "candidate_source_missing"
        operator_action = (
            "No rule-engine candidate rows exist. Run all_advisory.sh and verify advisory_candidates persistence before diagnosing missing BUYs."
        )
    else:
        code = "candidate_source_unavailable"
        operator_action = (
            "Rule-engine candidate source availability could not be verified. Check database/table health and rerun diagnostics before changing policy."
        )
    if status == "candidate_rows_missing" and int(candidate_source_availability.get("latest_candidate_rows") or 0) <= 0:
        advisory_command = "./all_advisory.sh"
    else:
        advisory_command = advisory_rerun_command(candidate_source_availability)
    diagnostic_command = recommendation_diagnostics_command(
        candidate_source_availability.get("latest_trading_asof_date")
        or candidate_source_availability.get("candidate_lookup_asof_date")
        or candidate_source_availability.get("requested_asof_date")
    )
    commands = [
        {
            "command": advisory_command,
            "purpose": "Regenerate rule-engine candidates and consolidated recommendations for the relevant trading date.",
        },
        {
            "command": diagnostic_command,
            "purpose": "Confirm candidate rows now exist for the diagnostic date before any threshold, regime, context, or LLM-policy tuning.",
        },
    ]
    return {
        "code": code,
        "severity": "blocker",
        "operator_action": operator_action,
        "evidence": evidence,
        "safe_to_tune_policy": False,
        "eligible_for_threshold_research": False,
        "next_commands": commands,
    }


def classify_no_data_primary_cause(
    *,
    asof_date: pd.Timestamp | None = None,
    diagnostic_trust: dict[str, Any] | None = None,
    candidate_source_availability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    diagnostic_trust = diagnostic_trust if isinstance(diagnostic_trust, dict) else {}
    candidate_source_availability = candidate_source_availability if isinstance(candidate_source_availability, dict) else {}
    parsed_asof = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date is not None else None
    parsed_asof = None if parsed_asof is not None and pd.isna(parsed_asof) else parsed_asof
    candidate_status = str(candidate_source_availability.get("status") or "").strip()
    if candidate_status in {"candidate_rows_missing_for_asof_latest_exists", "candidate_rows_missing", "candidate_source_unavailable", "candidate_source_availability_failed"}:
        return _candidate_source_primary_cause(candidate_source_availability)
    evidence: dict[str, Any] = {
        "requested_asof_date": None if parsed_asof is None else parsed_asof.isoformat(),
        "diagnostic_trust_status": diagnostic_trust.get("status"),
        "diagnostic_trust_reasons": diagnostic_trust.get("reasons", []),
    }
    commands = [
        {
            "command": "./all_advisory.sh",
            "purpose": "Generate and persist action recommendations for the latest advisory date.",
        },
        {
            "command": "python -m advisory.recommendation_diagnostics --format text",
            "purpose": "Confirm recommendations now exist and inspect the first actionable no-BUY cause.",
        },
    ]
    if parsed_asof is not None:
        date_text = parsed_asof.date().isoformat()
        commands.insert(
            1,
            {
                "command": f"./all_advisory.sh --date {date_text}",
                "purpose": "Generate recommendations for the requested as-of date when a dated rerun is intended.",
            },
        )
    return {
        "code": "no_persisted_action_rows",
        "severity": "blocker",
        "operator_action": "Run all_advisory.sh to generate action recommendations before diagnosing thresholds, regime/context policy, or BUY scarcity.",
        "evidence": evidence,
        "safe_to_tune_policy": False,
        "next_commands": commands,
    }


def _no_data_operator_next_steps(primary_no_buy_cause: dict[str, Any]) -> list[str]:
    primary_no_buy_cause = primary_no_buy_cause if isinstance(primary_no_buy_cause, dict) else {}
    operator_action = str(primary_no_buy_cause.get("operator_action") or "").strip()
    source = str(primary_no_buy_cause.get("next_commands_source") or "").strip()
    if source == "context_to_entry_pipeline_then_advisory_rerun_readiness":
        return [
            operator_action or "Run the context-to-entry repair command chain before diagnosing no-BUY behavior.",
            "After the context-to-entry repair chain finishes, rerun recommendation diagnostics before changing thresholds, regime/context policy, or recommendation rules.",
        ]
    return [
        operator_action or "Run all_advisory.sh to generate action recommendations before diagnosing thresholds, regime/context policy, or BUY scarcity.",
        "After the advisory run finishes, rerun recommendation diagnostics before changing thresholds, regime/context policy, or recommendation rules.",
    ]


def align_action_queue_authority_to_primary_cause(
    action_queue_authority_state: dict[str, Any] | None,
    primary_no_buy_cause: dict[str, Any] | None,
) -> dict[str, Any]:
    state = action_queue_authority_state if isinstance(action_queue_authority_state, dict) else {}
    primary = primary_no_buy_cause if isinstance(primary_no_buy_cause, dict) else {}
    commands = primary.get("next_commands") if isinstance(primary.get("next_commands"), list) else []
    if (
        str(primary.get("next_commands_source") or "") != "context_to_entry_pipeline_then_advisory_rerun_readiness"
        or not commands
    ):
        return state
    operator_action = str(state.get("operator_action") or "").strip()
    if "all_advisory" not in operator_action:
        return state
    aligned = {**state}
    aligned["operator_action"] = (
        "Action Queue rows are mixed or candidate freshness is unclear; use the primary context-to-entry repair command chain before policy tuning."
    )
    aligned["operator_action_source"] = "primary_no_buy_context_to_entry_commands"
    aligned["primary_no_buy_next_commands_source"] = primary.get("next_commands_source")
    aligned["primary_no_buy_first_command"] = _command_text(commands[0]) if commands else None
    return aligned


def classify_primary_no_buy_cause(
    *,
    positive_count: int,
    suppression_counts: Counter[str] | dict[str, int] | None,
    upstream: dict[str, Any] | None = None,
    handoff: dict[str, Any] | None = None,
    diagnostic_trust: dict[str, Any] | None = None,
    action_counts: Counter[str] | dict[str, int] | None = None,
    context_overlay_pressure: dict[str, Any] | None = None,
    post_advisory_activity: dict[str, Any] | None = None,
    market_context_blocks: dict[str, Any] | None = None,
    technical_artifacts: dict[str, Any] | None = None,
    context_overlay_refresh_preview: dict[str, Any] | None = None,
    context_overlay_signal_refresh_state: dict[str, Any] | None = None,
    causal_memory_refresh_preview: dict[str, Any] | None = None,
    market_participation_context: dict[str, Any] | None = None,
    candidate_source_availability: dict[str, Any] | None = None,
    transition_policy_gates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    upstream = upstream if isinstance(upstream, dict) else {}
    handoff = handoff if isinstance(handoff, dict) else {}
    diagnostic_trust = diagnostic_trust if isinstance(diagnostic_trust, dict) else {}
    context_overlay_pressure = context_overlay_pressure if isinstance(context_overlay_pressure, dict) else {}
    post_advisory_activity = post_advisory_activity if isinstance(post_advisory_activity, dict) else {}
    market_context_blocks = market_context_blocks if isinstance(market_context_blocks, dict) else {}
    technical_artifacts = technical_artifacts if isinstance(technical_artifacts, dict) else {}
    context_overlay_refresh_preview = context_overlay_refresh_preview if isinstance(context_overlay_refresh_preview, dict) else {}
    context_overlay_signal_refresh_state = (
        context_overlay_signal_refresh_state if isinstance(context_overlay_signal_refresh_state, dict) else {}
    )
    causal_memory_refresh_preview = causal_memory_refresh_preview if isinstance(causal_memory_refresh_preview, dict) else {}
    market_participation_context = summarize_market_participation_context(market_participation_context)
    candidate_source_availability = candidate_source_availability if isinstance(candidate_source_availability, dict) else {}
    transition_policy_gates = transition_policy_gates if isinstance(transition_policy_gates, dict) else {}
    suppression_counts = suppression_counts or {}
    action_counts = action_counts or {}
    fast_refresh_pending = bool(post_advisory_activity.get("action_refresh_recommended")) and int(
        post_advisory_activity.get("action_refresh_candidate_count") or 0
    ) > 0
    full_advisory_pending = (
        int(post_advisory_activity.get("full_advisory_required_count") or 0) > 0
        and not fast_refresh_pending
    )
    fast_refresh_command = str(post_advisory_activity.get("action_refresh_command") or "python -m advisory.action_recommender --format text")
    recommended_path = (
        post_advisory_activity.get("recommended_path")
        if isinstance(post_advisory_activity.get("recommended_path"), dict)
        else {}
    )
    context_refresh_preview_rows = int(context_overlay_refresh_preview.get("signal_rows") or 0)
    context_refresh_command = str(
        context_overlay_refresh_preview.get("recommended_command")
        or "python -m advisory.signal_refresh --from-context-overlays --limit 50 --format text"
    )
    causal_memory_preview_rows = int(causal_memory_refresh_preview.get("signal_rows") or 0)
    causal_memory_refresh_command = str(
        causal_memory_refresh_preview.get("recommended_command")
        or "python -m advisory.signal_refresh --from-causal-memory --limit 25 --format text"
    )
    bounded_preview_commands: list[dict[str, str]] = []
    if context_refresh_preview_rows > 0 and not bool(context_overlay_signal_refresh_state.get("current_for_preview")):
        bounded_preview_commands.append(
            {
                "command": context_refresh_command,
                "purpose": "Persist review-only context-overlay signal-refresh rows detected by the read-only preview; no portfolio or broker authority is granted.",
            }
        )
    if causal_memory_preview_rows > 0:
        bounded_preview_commands.append(
            {
                "command": causal_memory_refresh_command,
                "purpose": "Persist review-only causal-memory signal-refresh rows detected by the read-only preview; no portfolio or broker authority is granted.",
            }
        )
    stale_technical_artifact_rerun = (
        str(technical_artifacts.get("status") or "").strip() == "stale_pass_now_technical_ignore_artifacts"
        and not bounded_preview_commands
    )
    command_asof = advisory_readiness_command_asof(candidate_source_availability)
    advisory_rerun = (
        default_latest_advisory_rerun_command()
        if stale_technical_artifact_rerun
        else advisory_rerun_command(candidate_source_availability)
    )
    diagnostics_rerun = recommendation_diagnostics_command(command_asof)
    if bounded_preview_commands:
        diagnostic_rerun_commands = [
            *bounded_preview_commands,
            {
                "command": advisory_rerun,
                "purpose": "Regenerate authoritative recommendations with the current decision code and context-gate policy.",
            },
            {
                "command": diagnostics_rerun,
                "purpose": "Re-check the missing-BUY cause after the fresh advisory run finishes.",
            },
        ]
    elif fast_refresh_pending:
        diagnostic_rerun_commands = [
            {
                "command": fast_refresh_command,
                "purpose": "Update review-only Action Queue visibility from fresh watcher evidence before the long advisory reconciliation; no portfolio or broker authority is granted.",
            },
            {
                "command": advisory_rerun,
                "purpose": "Regenerate authoritative recommendations with the current decision code and context-gate policy.",
            },
            {
                "command": diagnostics_rerun,
                "purpose": "Re-check the missing-BUY cause after the fresh advisory run finishes.",
            },
        ]
    else:
        diagnostic_rerun_commands = [
            {
                "command": advisory_rerun,
                "purpose": "Regenerate recommendations with the current decision code and context-gate policy.",
            },
            {
                "command": diagnostics_rerun,
                "purpose": "Re-check the missing-BUY cause after the fresh advisory run finishes.",
            },
        ]
    if positive_count > 0:
        return {
            "code": "positive_recommendations_available",
            "severity": "info",
            "operator_action": "Positive BUY/BUY_MORE rows exist; inspect broker_candidate_count and transition preconditions before execution.",
            "safe_to_tune_policy": False,
            "next_commands": [
                {
                    "command": "python -m advisory.recommendation_diagnostics --format json",
                    "purpose": "Inspect full broker-candidate and transition-precondition evidence before any execution review.",
                }
            ],
        }
    candidate_status = str(candidate_source_availability.get("status") or "").strip()
    if candidate_status in {"candidate_rows_missing_for_asof_latest_exists", "candidate_rows_missing", "candidate_source_unavailable", "candidate_source_availability_failed"}:
        return _candidate_source_primary_cause(candidate_source_availability)
    if diagnostic_trust.get("status") == "requires_advisory_rerun":
        if bounded_preview_commands:
            operator_action = (
                "Run the bounded review-only signal refresh command(s) first for Action Queue visibility, "
                f"then rerun {advisory_rerun} for authoritative portfolio/risk/lifecycle reconciliation before changing policy."
            )
        else:
            operator_action = str(
                diagnostic_trust.get("operator_action")
                or f"Rerun {advisory_rerun} before interpreting the missing BUY state or changing policy."
            )
            if advisory_rerun != "./all_advisory.sh":
                if "`./all_advisory.sh`" in operator_action:
                    operator_action = operator_action.replace("`./all_advisory.sh`", f"`{advisory_rerun}`")
                elif "./all_advisory.sh" in operator_action:
                    operator_action = operator_action.replace("./all_advisory.sh", advisory_rerun)
                else:
                    operator_action = operator_action.replace("all_advisory.sh", advisory_rerun)
        return {
            "code": "diagnostic_requires_advisory_rerun",
            "severity": "blocker",
            "operator_action": operator_action,
            "evidence": {
                "trust_reasons": diagnostic_trust.get("reasons", []),
                "stale_context_gate_conflict": bool(diagnostic_trust.get("stale_context_gate_conflict")),
                "hard_context_gate_count": int(diagnostic_trust.get("hard_context_gate_count") or 0),
                "stale_hard_context_gate_count": int(diagnostic_trust.get("stale_hard_context_gate_count") or 0),
                "active_hard_context_gate_count": int(diagnostic_trust.get("active_hard_context_gate_count") or 0),
                "hard_context_gate_counts_by_type": diagnostic_trust.get("hard_context_gate_counts_by_type", {}),
                "stale_hard_context_gate_counts_by_type": diagnostic_trust.get("stale_hard_context_gate_counts_by_type", {}),
                "active_hard_context_gate_counts_by_type": diagnostic_trust.get("active_hard_context_gate_counts_by_type", {}),
                "post_advisory_fast_refresh_pending": bool(fast_refresh_pending),
                "action_refresh_candidate_count": int(post_advisory_activity.get("action_refresh_candidate_count") or 0),
                "action_refresh_candidate_counts": post_advisory_activity.get("action_refresh_candidate_counts", {}),
                "recommended_path_code": recommended_path.get("code"),
                "recommended_path_immediate_action": recommended_path.get("immediate_action"),
                "recommended_path_follow_up_action": recommended_path.get("follow_up_action"),
                "pass_now_with_technical_ignore_count": int(upstream.get("pass_now_with_technical_ignore_count") or 0),
                "pass_now_with_technical_ignore_samples": upstream.get("pass_now_with_technical_ignore_samples", []),
                "technical_entry_readiness": upstream.get("technical_entry_readiness", {}),
                "technical_artifacts": technical_artifacts,
                "context_overlay_refresh_preview": {
                    "status": context_overlay_refresh_preview.get("status"),
                    "target_rows": int(context_overlay_refresh_preview.get("target_rows") or 0),
                    "signal_rows": context_refresh_preview_rows,
                    "affected_symbol_count": int(context_overlay_refresh_preview.get("affected_symbol_count") or 0),
                    "raw_macro_context_target_rows": int(context_overlay_refresh_preview.get("raw_macro_context_target_rows") or 0),
                    "macro_context_target_rows": int(context_overlay_refresh_preview.get("macro_context_target_rows") or 0),
                    "positive_watch_target_rows": int(context_overlay_refresh_preview.get("positive_watch_target_rows") or 0),
                    "policy_boundary": context_overlay_refresh_preview.get("policy_boundary", {}),
                },
                "causal_memory_refresh_preview": {
                    "status": causal_memory_refresh_preview.get("status"),
                    "target_rows": int(causal_memory_refresh_preview.get("target_rows") or 0),
                    "signal_rows": causal_memory_preview_rows,
                    "affected_symbol_count": int(causal_memory_refresh_preview.get("affected_symbol_count") or 0),
                    "min_decayed_pressure_score": causal_memory_refresh_preview.get("min_decayed_pressure_score"),
                    "lookback_days": causal_memory_refresh_preview.get("lookback_days"),
                    "policy_boundary": causal_memory_refresh_preview.get("policy_boundary", {}),
                },
                "market_participation_context": market_participation_context,
            },
            "safe_to_tune_policy": False,
            "next_commands": diagnostic_rerun_commands,
        }
    if diagnostic_trust.get("status") == "limited_evidence":
        return {
            "code": "diagnostic_limited_evidence",
            "severity": "warning",
            "operator_action": str(
                diagnostic_trust.get("operator_action")
                or "Treat this diagnostic as partial; inspect load_ts, cron logs, and source tables before changing policy."
            ),
            "evidence": {"trust_reasons": diagnostic_trust.get("reasons", [])},
            "safe_to_tune_policy": False,
            "next_commands": [
                {
                    "command": "python -m advisory.recommendation_diagnostics --format json",
                    "purpose": "Inspect missing timestamp/source evidence before changing policy.",
                }
            ],
        }
    if fast_refresh_pending:
        return {
            "code": "post_advisory_watcher_action_refresh_pending",
            "severity": "actionable",
            "operator_action": (
                "Watcher or wait-signal evidence arrived after the latest advisory and can update review-only Action Queue rows quickly. "
                "Run the bounded action refresh before interpreting missing BUY rows or changing regime/context policy."
            ),
            "evidence": {
                "action_refresh_candidate_count": int(post_advisory_activity.get("action_refresh_candidate_count") or 0),
                "action_refresh_candidate_counts": post_advisory_activity.get("action_refresh_candidate_counts", {}),
                "action_refresh_sync_status": (
                    post_advisory_activity.get("action_refresh_sync", {}).get("status")
                    if isinstance(post_advisory_activity.get("action_refresh_sync"), dict)
                    else None
                ),
                "full_advisory_required_count": int(post_advisory_activity.get("full_advisory_required_count") or 0),
                "full_advisory_rerun_recommended": bool(post_advisory_activity.get("full_advisory_rerun_recommended")),
                "recommended_path_code": recommended_path.get("code"),
                "recommended_path_immediate_action": recommended_path.get("immediate_action"),
                "recommended_path_follow_up_action": recommended_path.get("follow_up_action"),
            },
            "safe_to_tune_policy": False,
            "next_commands": [
                {
                    "command": fast_refresh_command,
                    "purpose": "Refresh consolidated review-only Action Queue visibility from watcher evidence without granting portfolio or broker authority.",
                },
                {
                    "command": "python -m advisory.recommendation_diagnostics --format text",
                    "purpose": "Confirm the bounded action refresh sync is current before deciding whether full advisory reconciliation is still needed.",
                },
            ],
        }
    if full_advisory_pending:
        return {
            "code": "post_advisory_full_reconciliation_pending",
            "severity": "actionable",
            "operator_action": (
                "Watcher or wait-signal evidence arrived after the latest advisory, but it is not covered by bounded fast refresh. "
                "Run full advisory reconciliation before interpreting missing BUY rows or changing regime/context policy."
            ),
            "evidence": {
                "total_new_rows": int(post_advisory_activity.get("total_new_rows") or 0),
                "full_advisory_required_count": int(post_advisory_activity.get("full_advisory_required_count") or 0),
                "action_refresh_candidate_count": int(post_advisory_activity.get("action_refresh_candidate_count") or 0),
                "action_refresh_candidate_counts": post_advisory_activity.get("action_refresh_candidate_counts", {}),
                "recommended_path_code": recommended_path.get("code"),
                "recommended_path_immediate_action": recommended_path.get("immediate_action"),
                "recommended_path_follow_up_action": recommended_path.get("follow_up_action"),
                "samples": post_advisory_activity.get("samples", [])[:10],
            },
            "safe_to_tune_policy": False,
            "next_commands": [
                {
                    "command": "./all_advisory.sh",
                    "purpose": "Reconcile watcher and wait-signal evidence through full advisory, risk, portfolio, lifecycle, and action consolidation stages.",
                },
                {
                    "command": "python -m advisory.recommendation_diagnostics --format text",
                    "purpose": "Re-check the missing-BUY cause after full advisory reconciliation finishes.",
                },
            ],
        }
    if int(handoff.get("risk_base_unconfirmed_count") or 0) > 0 or int(handoff.get("portfolio_technical_entry_not_confirmed_count") or 0) > 0:
        return {
            "code": "risk_or_portfolio_waiting_for_technical_entry_confirmation",
            "severity": "actionable",
            "operator_action": "Inspect risk/portfolio handoff samples; candidates are being kept watch-only until entry triggers confirm.",
            "evidence": {
                "risk_base_unconfirmed_count": int(handoff.get("risk_base_unconfirmed_count") or 0),
                "portfolio_technical_entry_not_confirmed_count": int(handoff.get("portfolio_technical_entry_not_confirmed_count") or 0),
            },
            "safe_to_tune_policy": False,
            "next_commands": [
                {
                    "command": "python -m advisory.recommendation_diagnostics --format json",
                    "purpose": "Inspect handoff samples and technical-entry confirmation before loosening thresholds.",
                }
            ],
        }
    if (
        int(transition_policy_gates.get("transition_policy_gate_count") or 0) > 0
        and transition_gates_include_entry_actions(transition_policy_gates)
    ):
        return {
            "code": "transition_stability_policy_gate_downgraded_broker_actions",
            "severity": "actionable",
            "operator_action": (
                "Broker-capable rows were downgraded by the action-transition stability gate. "
                "Inspect transition blockers and rerun full advisory only after fresh technical, lifecycle, risk, and position evidence is available."
            ),
            "evidence": {
                "transition_policy_gate_count": int(transition_policy_gates.get("transition_policy_gate_count") or 0),
                "original_action_counts": transition_policy_gates.get("original_action_counts", {}),
                "downgraded_action_counts": transition_policy_gates.get("downgraded_action_counts", {}),
                "transition_blocker_counts": transition_policy_gates.get("transition_blocker_counts", {}),
                "samples": transition_policy_gates.get("samples", [])[:10],
            },
            "safe_to_tune_policy": False,
            "eligible_for_threshold_research": False,
            "next_commands": [
                {
                    "command": "python -m advisory.recommendation_diagnostics --format json",
                    "purpose": "Inspect transition_policy_gates samples and blockers before changing thresholds or context policy.",
                },
                {
                    "command": "./all_advisory.sh",
                    "purpose": "Regenerate authoritative transition evidence after data, lifecycle, and technical inputs are current.",
                },
            ],
        }
    if int(upstream.get("pass_now_with_technical_ignore_count") or 0) > 0:
        technical_readiness = upstream.get("technical_entry_readiness") if isinstance(upstream.get("technical_entry_readiness"), dict) else {}
        technical_artifact_status = str(technical_artifacts.get("status") or "").strip()
        if technical_artifact_status == "current_pass_now_technical_ignore_conflict":
            next_commands = [
                {
                    "command": "python -m advisory.recommendation_diagnostics --format json",
                    "purpose": (
                        "Inspect current PASS_NOW technical-ignore samples, candidate states, and rule-engine evidence "
                        "before changing thresholds or regime/context policy."
                    ),
                },
                {
                    "command": "./all_advisory.sh",
                    "purpose": "Rerun full advisory after inspecting whether current rule-engine scoring unexpectedly bypassed technical-entry confirmation.",
                },
                {
                    "command": "python -m advisory.recommendation_diagnostics --format text",
                    "purpose": "Verify the technical-confirmation bypass count after inspection and rerun.",
                },
            ]
        elif technical_artifact_status == "stale_pass_now_technical_ignore_artifacts":
            next_commands = [
                {
                    "command": "./all_advisory.sh",
                    "purpose": "Clear stale PASS_NOW technical-ignore artifacts with a fresh run before tuning thresholds or context policy.",
                },
                {
                    "command": "python -m advisory.recommendation_diagnostics --format text",
                    "purpose": "Verify whether PASS_NOW technical-ignore rows still exist after the fresh run.",
                },
            ]
        else:
            next_commands = [
                {
                    "command": "./all_advisory.sh",
                    "purpose": "Confirm whether PASS_NOW with technical_state=IGNORE survives a fresh run.",
                },
                {
                    "command": "python -m advisory.recommendation_diagnostics --format text",
                    "purpose": "Verify the technical-confirmation bypass count after rerun.",
                },
            ]
        return {
            "code": "upstream_pass_now_without_technical_confirmation",
            "severity": "bug_or_stale_rows",
            "operator_action": str(
                technical_artifacts.get("operator_action")
                or "Verify a fresh advisory run; PASS_NOW with technical_state=IGNORE means stale rows or setup scoring bypassed entry-trigger confirmation."
            ),
            "evidence": {
                "pass_now_with_technical_ignore_count": int(upstream.get("pass_now_with_technical_ignore_count") or 0),
                "technical_state_counts": upstream.get("technical_state_counts", {}),
                "candidate_state_counts": upstream.get("candidate_state_counts", {}),
                "technical_entry_readiness_diagnosis": technical_readiness.get("diagnosis"),
                "confirmed_entry_candidate_count": int(technical_readiness.get("confirmed_entry_candidate_count") or 0),
                "ignore_or_reject_candidate_count": int(technical_readiness.get("ignore_or_reject_candidate_count") or 0),
                "technical_artifacts": technical_artifacts,
                "samples": upstream.get("pass_now_with_technical_ignore_samples", []),
            },
            "safe_to_tune_policy": False,
            "next_commands": next_commands,
        }
    technical_readiness = upstream.get("technical_entry_readiness") if isinstance(upstream.get("technical_entry_readiness"), dict) else {}
    if (
        int(technical_readiness.get("positive_or_constructive_candidate_count") or 0) > 0
        and int(technical_readiness.get("confirmed_entry_candidate_count") or 0) == 0
        and int(suppression_counts.get("feature_freshness_blocked", 0)) == 0
    ):
        threshold_research_plan = build_technical_threshold_research_plan(
            candidate_source_availability=candidate_source_availability,
            technical_readiness=technical_readiness,
        )
        return {
            "code": "technical_entry_confirmation_not_met",
            "severity": "actionable",
            "operator_action": (
                "Constructive candidates exist, but none have confirmed technical entry states. "
                "Treat this as technical underparticipation research, not a regime/global-context policy problem."
            ),
            "evidence": {
                "positive_or_constructive_candidate_count": int(technical_readiness.get("positive_or_constructive_candidate_count") or 0),
                "watch_or_near_entry_candidate_count": int(technical_readiness.get("watch_or_near_entry_candidate_count") or 0),
                "ignore_or_reject_candidate_count": int(technical_readiness.get("ignore_or_reject_candidate_count") or 0),
                "technical_entry_readiness_diagnosis": technical_readiness.get("diagnosis"),
                "technical_state_counts": upstream.get("technical_state_counts", {}),
                "technical_trigger_counts": upstream.get("technical_trigger_counts", {}),
                "technical_trigger_blockers": technical_readiness.get("technical_trigger_blockers", {}),
                "buy_readiness_blockers": technical_readiness.get("buy_readiness_blockers", {}),
                "top_soft_failures": technical_readiness.get("top_soft_failures", []),
                "soft_failure_counts": technical_readiness.get("soft_failure_counts", {}),
                "market_participation_context": market_participation_context,
                "technical_threshold_research_plan": threshold_research_plan,
            },
            "safe_to_tune_policy": False,
            "eligible_for_threshold_research": bool(threshold_research_plan.get("eligible_for_threshold_research")),
            "next_commands": threshold_research_plan.get("next_commands", []),
        }
    if int(suppression_counts.get("feature_freshness_blocked", 0)) > 0:
        return {
            "code": "required_features_blocked_or_stale",
            "severity": "actionable",
            "operator_action": "Refresh required data inputs before treating missing BUYs as a model or policy problem.",
            "evidence": {"feature_freshness_blocked": int(suppression_counts.get("feature_freshness_blocked", 0))},
            "safe_to_tune_policy": False,
            "next_commands": [
                {
                    "command": "./complete_data.sh",
                    "purpose": "Refresh required data inputs and feature sources.",
                },
                {
                    "command": "./all_advisory.sh",
                    "purpose": "Regenerate recommendations after data refresh.",
                },
            ],
        }
    if int(suppression_counts.get("market_context_blocked", 0)) > 0:
        return {
            "code": "market_context_suppressed_positive_actions",
            "severity": "review",
            "operator_action": "Inspect hard macro-risk, breadth, and context-overlay evidence; do not tune global regime flags without symbol-level evidence.",
            "evidence": {
                "market_context_blocked": int(suppression_counts.get("market_context_blocked", 0)),
                "block_reason_counts": market_context_blocks.get("reason_counts", {}),
                "macro_risk_state_counts": market_context_blocks.get("macro_risk_state_counts", {}),
            },
            "safe_to_tune_policy": False,
            "next_commands": [
                {
                    "command": "python -m advisory.recommendation_diagnostics --format json",
                    "purpose": "Inspect market-context and context-overlay evidence before changing any gate.",
                }
            ],
        }
    if int(upstream.get("confirmed_entry_lost_downstream_count") or 0) > 0:
        technical_readiness = upstream.get("technical_entry_readiness") if isinstance(upstream.get("technical_entry_readiness"), dict) else {}
        return {
            "code": "confirmed_technical_candidates_lost_downstream",
            "severity": "review",
            "operator_action": (
                "Confirmed technical entry candidates existed, but they did not survive action/risk/portfolio/lifecycle consolidation. "
                "Inspect final-action distribution and samples before changing regime/context or technical thresholds."
            ),
            "evidence": {
                "confirmed_entry_candidate_count": int(technical_readiness.get("confirmed_entry_candidate_count") or 0),
                "confirmed_entry_lost_downstream_count": int(upstream.get("confirmed_entry_lost_downstream_count") or 0),
                "confirmed_entry_final_action_counts": upstream.get("confirmed_entry_final_action_counts", {}),
                "samples": upstream.get("confirmed_entry_lost_downstream_samples", []),
            },
            "safe_to_tune_policy": False,
            "next_commands": [
                {
                    "command": "python -m advisory.recommendation_diagnostics --format json",
                    "purpose": "Inspect confirmed technical candidates that were downgraded by action/risk/portfolio/lifecycle consolidation.",
                },
                {
                    "command": "./all_advisory.sh",
                    "purpose": "Rerun full advisory only after verifying the latest rows are stale or the downstream loss is unintended.",
                },
            ],
        }
    if _exit_only_action_mix(action_counts):
        return {
            "code": "exit_only_action_mix_no_entry_candidates",
            "severity": "info",
            "operator_action": (
                "The latest action set contains only exit/risk-reduction actions. "
                "After a portfolio reset or with no open positions, treat these as non-entry housekeeping signals; "
                "look at upstream candidates, watch rows, and technical-entry confirmation for new BUY generation."
            ),
            "evidence": {
                "action_counts": dict(action_counts),
                "exit_or_risk_reduction_actions": sorted(EXIT_OR_RISK_REDUCTION_ACTIONS),
            },
            "safe_to_tune_policy": False,
            "eligible_for_threshold_research": True,
            "next_commands": [
                {
                    "command": "python -m advisory.recommendation_diagnostics --format json",
                    "purpose": "Inspect upstream candidates and confirm whether the latest recommendation set is exit-only.",
                },
                {
                    "command": "python -m advisory.technical_threshold_calibration --dry-run --horizons 5 10 20 --max-configs 512 --progress-every 128",
                    "purpose": "Research whether technical-entry triggers are missing profitable entry setups without changing live policy.",
                },
            ],
        }
    if int(upstream.get("positive_or_constructive_candidate_count") or 0) > 0:
        return {
            "code": "constructive_candidates_did_not_survive_consolidation",
            "severity": "review",
            "operator_action": "Inspect candidate-to-final-action survival and conflict rules to see why constructive rows became watch/review/exit actions.",
            "evidence": {
                "positive_or_constructive_candidate_count": int(upstream.get("positive_or_constructive_candidate_count") or 0),
                "positive_candidate_final_action_counts": upstream.get("positive_candidate_final_action_counts", {}),
            },
            "safe_to_tune_policy": False,
            "next_commands": [
                {
                    "command": "python -m advisory.recommendation_diagnostics --format json",
                    "purpose": "Inspect conflict and final-action survival evidence.",
                }
            ],
        }
    if int(context_overlay_pressure.get("positive_or_watch_pressure_count") or 0) > 0 and int(action_counts.get("WATCH", 0)) > 0:
        return {
            "code": "context_overlay_watch_pressure_waiting_for_technical_confirmation",
            "severity": "info",
            "operator_action": (
                "News/macro/announcement/bhavcopy context created watch pressure, but it remains review-only. "
                "Do not convert it into BUY authority until technical entry, liquidity, risk, lifecycle, and execution gates confirm."
            ),
            "evidence": {
                "positive_or_watch_context_pressure_count": int(context_overlay_pressure.get("positive_or_watch_pressure_count") or 0),
                "negative_context_pressure_count": int(context_overlay_pressure.get("negative_pressure_count") or 0),
                "context_overlay_status": context_overlay_pressure.get("status"),
                "context_overlay_action_counts": context_overlay_pressure.get("row_action_counts", {}),
                "watch_count": int(action_counts.get("WATCH", 0)),
            },
            "safe_to_tune_policy": False,
            "eligible_for_threshold_research": True,
            "next_commands": [
                {
                    "command": "python -m advisory.recommendation_diagnostics --format json",
                    "purpose": "Inspect context_overlay_pressure samples and confirm context rows are only watch/de-risk evidence.",
                },
                {
                    "command": "python -m advisory.technical_threshold_calibration --dry-run --horizons 5 10 20 --max-configs 512 --progress-every 128",
                    "purpose": "Research whether technical entry thresholds are missing profitable context-driven watch setups without changing live policy.",
                },
            ],
        }
    _confirmed_now = int((upstream.get("technical_entry_readiness") or {}).get("confirmed_entry_candidate_count") or 0) if isinstance(upstream, dict) else 0
    if _confirmed_now > 0:
        # We fell through every specific diagnosis (lost-downstream, consolidation, freshness, context...)
        # yet technically-confirmed entry candidates exist and produced no BUY. action_recommender emits
        # BUY only from an already-approved portfolio row (advisory/action_recommender.py:6512): there is
        # no promotion bridge candidate->approved->BUY (the human/[P-LLM-AUTH] step is unbuilt). Name the
        # architectural gap explicitly instead of blaming a policy/threshold.
        return {
            "code": "confirmed_entry_exists_but_no_promotion_bridge",
            "severity": "architectural_gap",
            "operator_action": (
                "Confirmed technical entry candidates exist but no BUY was produced: the funnel has no "
                "promotion bridge candidate->approved->BUY (BUY is emitted only from approved portfolio "
                "rows). Build the promotion/approval step ([P-LLM-AUTH]); this is not a policy/threshold gap."
            ),
            "evidence": {
                "confirmed_entry_candidate_count": _confirmed_now,
                "positive_recommendation_count": int(positive_count or 0),
                "candidate_state_counts": upstream.get("candidate_state_counts", {}) if isinstance(upstream, dict) else {},
                "technical_state_counts": upstream.get("technical_state_counts", {}) if isinstance(upstream, dict) else {},
            },
            "safe_to_tune_policy": False,
            "next_commands": [],
        }
    if int(action_counts.get("WATCH", 0)) > 0:
        return {
            "code": "watch_only_no_entry_trigger",
            "severity": "info",
            "operator_action": "Watch rows exist but no BUY trigger confirmed; wait for entry trigger or run research-only technical calibration before considering threshold changes.",
            "evidence": {"watch_count": int(action_counts.get("WATCH", 0))},
            "safe_to_tune_policy": False,
            "eligible_for_threshold_research": True,
            "next_commands": [
                {
                    "command": "python -m advisory.recommendation_diagnostics --format json",
                    "purpose": "Inspect watch-only technical states before deciding whether thresholds need research.",
                },
                {
                    "command": "python -m advisory.technical_threshold_calibration --dry-run --horizons 5 10 20 --max-configs 512 --progress-every 128",
                    "purpose": "Research threshold alternatives without changing live policy, portfolio rows, or broker behavior.",
                }
            ],
        }
    return {
        "code": "no_positive_rows_unclassified",
        "severity": "review",
        "operator_action": "Inspect action mix, upstream candidates, and rejection reasons; the diagnostic did not find a dominant no-BUY cause.",
        "safe_to_tune_policy": False,
        "next_commands": [
            {
                "command": "python -m advisory.recommendation_diagnostics --format json",
                "purpose": "Inspect full diagnostic evidence before changing policy.",
            }
        ],
    }


def _operator_next_steps(
    *,
    positive_count: int,
    suppression_counts: Counter[str],
    broker_candidate_count: int,
    upstream: dict[str, Any] | None = None,
    handoff: dict[str, Any] | None = None,
    context_gate_policy: dict[str, Any] | None = None,
    code_data_freshness: dict[str, Any] | None = None,
    diagnostic_trust: dict[str, Any] | None = None,
    action_counts: Counter[str] | dict[str, int] | None = None,
    context_overlay_pressure: dict[str, Any] | None = None,
    context_overlay_source_diagnostics: dict[str, Any] | None = None,
    context_overlay_refresh_preview: dict[str, Any] | None = None,
    context_overlay_signal_refresh_state: dict[str, Any] | None = None,
    context_to_entry_pipeline: dict[str, Any] | None = None,
    signal_quality_attribution: dict[str, Any] | None = None,
    causal_memory_refresh_preview: dict[str, Any] | None = None,
    market_context_blocks: dict[str, Any] | None = None,
    post_advisory_activity: dict[str, Any] | None = None,
    technical_artifacts: dict[str, Any] | None = None,
    market_participation_context: dict[str, Any] | None = None,
    manual_review_breakdown: dict[str, Any] | None = None,
    candidate_source_availability: dict[str, Any] | None = None,
    transition_policy_gates: dict[str, Any] | None = None,
    action_queue_authority_state: dict[str, Any] | None = None,
    primary_no_buy_cause: dict[str, Any] | None = None,
) -> list[str]:
    upstream = upstream if isinstance(upstream, dict) else {}
    handoff = handoff if isinstance(handoff, dict) else {}
    context_gate_policy = context_gate_policy if isinstance(context_gate_policy, dict) else {}
    code_data_freshness = code_data_freshness if isinstance(code_data_freshness, dict) else {}
    diagnostic_trust = diagnostic_trust if isinstance(diagnostic_trust, dict) else {}
    action_counts = action_counts if isinstance(action_counts, (Counter, dict)) else {}
    context_overlay_pressure = context_overlay_pressure if isinstance(context_overlay_pressure, dict) else {}
    context_overlay_source_diagnostics = context_overlay_source_diagnostics if isinstance(context_overlay_source_diagnostics, dict) else {}
    context_overlay_refresh_preview = context_overlay_refresh_preview if isinstance(context_overlay_refresh_preview, dict) else {}
    context_overlay_signal_refresh_state = (
        context_overlay_signal_refresh_state if isinstance(context_overlay_signal_refresh_state, dict) else {}
    )
    context_to_entry_pipeline = context_to_entry_pipeline if isinstance(context_to_entry_pipeline, dict) else {}
    signal_quality_attribution = summarize_signal_quality_attribution(signal_quality_attribution)
    causal_memory_refresh_preview = causal_memory_refresh_preview if isinstance(causal_memory_refresh_preview, dict) else {}
    market_context_blocks = market_context_blocks if isinstance(market_context_blocks, dict) else {}
    post_advisory_activity = post_advisory_activity if isinstance(post_advisory_activity, dict) else {}
    technical_artifacts = technical_artifacts if isinstance(technical_artifacts, dict) else {}
    market_participation_context = summarize_market_participation_context(market_participation_context)
    manual_review_breakdown = manual_review_breakdown if isinstance(manual_review_breakdown, dict) else {}
    candidate_source_availability = candidate_source_availability if isinstance(candidate_source_availability, dict) else {}
    transition_policy_gates = transition_policy_gates if isinstance(transition_policy_gates, dict) else {}
    action_queue_authority_state = action_queue_authority_state if isinstance(action_queue_authority_state, dict) else {}
    primary_no_buy_cause = primary_no_buy_cause if isinstance(primary_no_buy_cause, dict) else {}
    source_family_attribution = (
        context_to_entry_pipeline.get("source_family_attribution")
        if isinstance(context_to_entry_pipeline.get("source_family_attribution"), dict)
        else {}
    )
    advisory_command = advisory_rerun_command(candidate_source_availability)
    primary_commands = (
        primary_no_buy_cause.get("next_commands")
        if isinstance(primary_no_buy_cause.get("next_commands"), list)
        else []
    )
    has_context_to_entry_command_chain = (
        str(primary_no_buy_cause.get("next_commands_source") or "")
        == "context_to_entry_pipeline_then_advisory_rerun_readiness"
        and bool(primary_commands)
    )

    def _date_aware_advisory_step(text: str) -> str:
        if not text:
            return text
        if advisory_command == "./all_advisory.sh":
            return text
        return (
            text.replace("`./all_advisory.sh`", f"`{advisory_command}`")
            .replace("`all_advisory.sh`", f"`{advisory_command}`")
            .replace("./all_advisory.sh", f"`{advisory_command}`")
            .replace("all_advisory.sh", f"`{advisory_command}`")
        )
    recommended_path = (
        post_advisory_activity.get("recommended_path")
        if isinstance(post_advisory_activity.get("recommended_path"), dict)
        else {}
    )
    action_refresh_recommended = bool(post_advisory_activity.get("action_refresh_recommended"))
    bounded_preview_available = (
        (
            int(context_overlay_refresh_preview.get("signal_rows") or 0) > 0
            and not bool(context_overlay_signal_refresh_state.get("current_for_preview"))
        )
        or int(causal_memory_refresh_preview.get("signal_rows") or 0) > 0
    )
    steps: list[str] = []
    if has_context_to_entry_command_chain:
        steps.append(
            "Context-to-entry diagnostics found a narrower repair path; run these commands in order before policy tuning: "
            f"{_format_command_sequence(primary_commands[:2])}."
        )
    if bounded_preview_available:
        steps.append(
            "A read-only bounded signal-refresh preview can create review-only Action Queue rows now; run the listed signal-refresh command(s) before the long full-advisory reconciliation."
        )
    if action_refresh_recommended and str(recommended_path.get("immediate_action") or "") == "run_action_refresh":
        steps.append(
            "Fresh watcher evidence can update review-only Action Queue rows now; run the bounded action refresh before waiting for the long full-advisory reconciliation."
        )
    elif (
        int(post_advisory_activity.get("full_advisory_required_count") or 0) > 0
        or str(recommended_path.get("immediate_action") or "") == "run_full_advisory"
    ):
        steps.append(
            f"Fresh watcher or wait-signal evidence requires full advisory reconciliation; run `{advisory_command}` before changing thresholds or context policy."
        )
    if str(action_queue_authority_state.get("status") or "") == "fast_review_only_queue_authoritative_advisory_missing":
        steps.append(
            f"The Action Queue is currently fast signal-refresh visibility only and same-date rule-engine candidates are missing/stale; run `{advisory_command}` before interpreting no-BUY as regime, LLM, or threshold behavior."
        )
    elif str(action_queue_authority_state.get("status") or "") == "fast_review_only_queue":
        steps.append(
            "The Action Queue is currently signal-refresh-only review evidence; use it for watch/de-risk visibility, not portfolio sizing or broker-candidate conclusions."
        )
    if diagnostic_trust.get("status") and diagnostic_trust.get("status") != "current":
        if has_context_to_entry_command_chain:
            steps.append(
                "Persisted advisory rows still need reconciliation, but use the context-to-entry repair path above before falling back to the long full-advisory run."
            )
        elif bounded_preview_available and diagnostic_trust.get("status") == "requires_advisory_rerun":
            steps.append(
                "Persisted advisory rows are stale, but bounded review-only signal refresh can improve Action Queue visibility before the required full advisory rerun."
            )
        else:
            steps.append(
                _date_aware_advisory_step(
                    str(diagnostic_trust.get("operator_action") or "Diagnostic trust is limited; verify freshness before changing policy.")
                )
            )
    if code_data_freshness.get("source_rows_precede_current_code"):
        if has_context_to_entry_command_chain:
            steps.append(
                "Persisted advisory rows were generated before the current decision code; the context-to-entry command sequence above includes bounded reconciliation, so run it before treating this report as current."
            )
        else:
            steps.append(
                f"Persisted advisory rows were generated before the current decision code; rerun `{advisory_command}` before treating this report as current."
            )
    if str(candidate_source_availability.get("status") or "") == "candidate_rows_missing_for_asof_latest_exists":
        if has_context_to_entry_command_chain:
            steps.append(
                "No rule-engine candidate rows exist for the diagnostic action date; first complete the context-to-entry repair path above, then rerun diagnostics before interpreting missing BUYs as regime, threshold, or context-policy behavior."
            )
        else:
            steps.append(
                f"No rule-engine candidate rows exist for the diagnostic action date, although another candidate date exists; run `{advisory_command}` before interpreting missing BUYs as regime, threshold, or context-policy behavior."
            )
    elif str(candidate_source_availability.get("status") or "") in {"candidate_rows_missing", "candidate_source_unavailable", "candidate_source_availability_failed"}:
        steps.append(
            "Rule-engine candidate source evidence is unavailable for this diagnostic date; verify advisory_candidates persistence before changing regime, threshold, or context policy."
        )
    if positive_count == 0:
        steps.append("No final BUY/BUY_MORE rows exist for this date; inspect top_blocked_or_review_symbols before changing thresholds.")
        if market_participation_context.get("market_participation_label") == "benchmark_uptrend":
            steps.append(
                "The benchmark participation context is positive, so missing BUYs are an underparticipation diagnostic; inspect technical entry confirmation, stale rows, context-overlay conversion, and downstream consolidation before changing any global regime setting."
            )
    if int(signal_quality_attribution.get("benchmark_or_attribution_blocked_count") or 0) > 0:
        steps.append(
            "Signal-quality context evidence is blocked by benchmark beta or missing attribution; run rolling-window split diagnostics before loosening regime/context policy."
        )
    top_family_blockers = _top_context_source_family_blockers(source_family_attribution, limit=3)
    if top_family_blockers:
        steps.append(
            "Context source-family attribution found source-specific gaps before action pressure: "
            f"{_format_context_source_family_blockers(source_family_attribution, limit=3)}. "
            "Fix the affected family conversion/reliability/watchlist path before changing global regime/context policy."
        )
    if int(upstream.get("positive_or_constructive_candidate_count") or 0) > 0:
        steps.append("Upstream constructive candidates exist; inspect upstream_candidates.positive_candidate_final_action_counts to see where they were downgraded.")
    if int(upstream.get("confirmed_entry_lost_downstream_count") or 0) > 0:
        steps.append(
            "Confirmed technical entry candidates were downgraded downstream; inspect upstream_candidates.confirmed_entry_final_action_counts and confirmed_entry_lost_downstream_samples before changing regime/context or technical thresholds."
        )
    if int(transition_policy_gates.get("transition_policy_gate_count") or 0) > 0:
        steps.append(
            "Some broker-capable rows were downgraded by action-transition stability; inspect transition_policy_gates before changing regime/context policy or technical thresholds."
        )
    if int(upstream.get("pass_now_with_technical_ignore_count") or 0) > 0:
        if technical_artifacts.get("status") == "stale_pass_now_technical_ignore_artifacts":
            steps.append(
                f"Some PASS_NOW candidates have technical_state=IGNORE, but they are stale pre-fix artifacts; rerun `{advisory_command}` before treating this as a current technical-threshold problem."
            )
        elif technical_artifacts.get("status") == "current_pass_now_technical_ignore_conflict":
            steps.append(
                "Fresh PASS_NOW candidates still have technical_state=IGNORE; inspect rule-engine setup scoring for a current technical-trigger bypass before changing regime/context policy."
            )
        else:
            steps.append("Some PASS_NOW candidates have technical_state=IGNORE; verify whether setup scoring is bypassing technical trigger confirmation.")
    technical_readiness = upstream.get("technical_entry_readiness") if isinstance(upstream.get("technical_entry_readiness"), dict) else {}
    if (
        int(technical_readiness.get("positive_or_constructive_candidate_count") or 0) > 0
        and int(technical_readiness.get("confirmed_entry_candidate_count") or 0) == 0
    ):
        if technical_artifacts.get("status") == "stale_pass_now_technical_ignore_artifacts":
            steps.append(
                f"Constructive candidates exist but current technical-entry evidence is stale; rerun `{advisory_command}` before running threshold calibration or changing regime/context policy."
            )
        elif technical_artifacts.get("status") == "current_pass_now_technical_ignore_conflict":
            steps.append(
                "Constructive candidates exist but PASS_NOW conflicts with technical_state=IGNORE; inspect rule-engine scoring before running threshold calibration or changing regime/context policy."
            )
        else:
            steps.append("Constructive candidates exist but no confirmed technical entry states exist; inspect technical_entry_readiness and run dry-run technical threshold calibration before changing regime/context policy.")
    regime_overlay = upstream.get("regime_overlay_rejections") if isinstance(upstream.get("regime_overlay_rejections"), dict) else {}
    rule_overlay_hard_now = bool(context_gate_policy.get("rule_engine_overlay_label_hard_block_enabled"))
    rule_regime_hard_now = bool(context_gate_policy.get("rule_engine_regime_label_hard_block_enabled"))
    if int(regime_overlay.get("hard_context_gate_count") or 0) > 0:
        if bool(diagnostic_trust.get("stale_context_gate_superseded_by_action_refresh")):
            steps.append(
                "Older persisted hard regime/overlay rejection rows were superseded by newer consolidated action recommendations; keep them as audit history, but diagnose missing BUYs from current action rows and technical/context evidence."
            )
        elif not rule_overlay_hard_now and not rule_regime_hard_now:
            steps.append(
                f"Persisted hard regime/overlay rejection rows conflict with current context-only rule-engine policy; rerun `{advisory_command}` before treating them as active blockers."
            )
        else:
            steps.append("Hard regime/overlay rejection rows exist; inspect upstream_candidates.regime_overlay_rejections and verify hard-block env flags are intentional.")
    elif int(regime_overlay.get("context_only_warning_count") or 0) > 0:
        steps.append("Regime/overlay rejection rows are context-only warnings, not hard missing-BUY blockers; prioritize technical confirmation and feature freshness first.")
    if int(handoff.get("risk_base_unconfirmed_count") or 0) > 0:
        steps.append("Risk allocations include base fallback rows without confirmed technical entries; inspect risk_portfolio_handoff.risk_base_unconfirmed_samples before loosening thresholds.")
    if int(handoff.get("portfolio_technical_entry_not_confirmed_count") or 0) > 0:
        steps.append("Portfolio rows were deferred because technical entry was not confirmed; inspect risk_portfolio_handoff.portfolio_technical_entry_not_confirmed_samples.")
    if suppression_counts.get("market_context_blocked"):
        market_reason_counts = market_context_blocks.get("reason_counts") if isinstance(market_context_blocks.get("reason_counts"), dict) else {}
        if int(market_reason_counts.get("legacy_regime_label_block") or 0) > 0:
            steps.append(
                f"Market-context blocks include legacy single-regime label artifacts; rerun `{advisory_command}` before treating them as active macro/score/breadth blockers."
            )
        else:
            steps.append(
                "Market context blocked positive actions; inspect market_context_blocks.reason_counts to separate hard macro-risk from score-only, weak-breadth, or regime-label blocks."
            )
    if suppression_counts.get("possible_single_regime_label_block"):
        steps.append("A broad regime label may still be acting as a hard gate; verify ACTION_MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED and related env flags.")
    if suppression_counts.get("feature_freshness_blocked"):
        steps.append("Required feature inputs are blocked or stale; refresh data before treating missing BUYs as a policy problem.")
    if suppression_counts.get("context_conflicts_positive"):
        steps.append("Context overlays conflict with positive actions; review source-family/class reliability before trusting the conflict.")
    manual_buckets = manual_review_breakdown.get("bucket_counts") if isinstance(manual_review_breakdown.get("bucket_counts"), dict) else {}
    manual_targets = manual_review_breakdown.get("automation_targets") if isinstance(manual_review_breakdown.get("automation_targets"), dict) else {}
    if int(manual_review_breakdown.get("total_manual_review_rows") or 0) > 0:
        steps.append(
            "Manual Review rows are categorized by automation target; inspect manual_review_breakdown before treating them as human investment decisions."
        )
    if int(manual_targets.get("refresh_data_or_prices") or 0) > 0:
        steps.append("Some Manual Review rows are operational data/price blockers; refresh OHLCV/current-price inputs before asking for human investment judgment.")
    if int(manual_targets.get("rerun_advisory_for_stale_context") or 0) > 0:
        steps.append("Some Manual Review rows are stale context artifacts; rerun advisory before asking humans to resolve them.")
    if int(manual_targets.get("improve_llm_policy_resolution") or 0) > 0:
        steps.append("Some Manual Review rows still need LLM/event-policy resolution; improve evaluator/playbook classification before relying on manual UI decisions.")
    if int(manual_buckets.get("llm_resolved_review_only") or 0) > 0:
        steps.append("Some event/playbook Manual Review rows are already LLM-resolved review-only watch/de-risk evidence; keep them out of broker or portfolio authority.")
    if positive_count == 0 and _exit_only_action_mix(action_counts):
        steps.append("The current action set appears exit-only; if the operator/paper portfolio was reset, do not treat those rows as new SELL recommendations.")
    if int(context_overlay_pressure.get("positive_or_watch_pressure_count") or 0) > 0:
        steps.append("Context overlays created positive/watch pressure, but this is watch/de-risk evidence only; inspect context_overlay_pressure and wait for technical/risk/lifecycle confirmation before expecting BUY rows.")
    if int(context_overlay_refresh_preview.get("signal_rows") or 0) > 0 and not bool(
        context_overlay_signal_refresh_state.get("current_for_preview")
    ):
        steps.append(
            "A read-only context-overlay refresh preview can create review-only Action Queue rows now; run the context-overlay signal-refresh command before the long full-advisory reconciliation."
        )
    if int(causal_memory_refresh_preview.get("signal_rows") or 0) > 0:
        steps.append(
            "A read-only causal-memory refresh preview can create review-only Action Queue rows now; run the causal-memory signal-refresh command before the long full-advisory reconciliation."
        )
    elif (
        positive_count == 0
        and context_overlay_pressure.get("status") == "no_context_overlay_pressure"
        and int(context_overlay_source_diagnostics.get("active_authorized_rows_total") or 0) > 0
    ):
        steps.append(
            "Context-overlay source rows existed for this date but did not appear in consolidated actions; inspect context_overlay_source_diagnostics and run signal-refresh from context overlays before treating context evidence as absent."
        )
    if broker_candidate_count == 0:
        steps.append("No complete broker-candidate rows exist; execution should remain disabled until reason contracts and transition preconditions are complete.")
    return steps or ["Positive recommendations exist; inspect broker_candidate_count and action transition preconditions before execution."]


def build_recommendation_diagnostics(*, asof_date: pd.Timestamp | None = None, limit: int = 500) -> dict[str, Any]:
    try:
        rows = load_recommendation_rows(asof_date=asof_date, limit=limit)
        effective_asof = asof_date
        if effective_asof is None and not rows.empty:
            parsed_asof = pd.to_datetime(rows["asof_date"].iloc[0], utc=True, errors="coerce")
            effective_asof = None if pd.isna(parsed_asof) else parsed_asof.normalize()
        candidate_source_availability = load_candidate_source_availability(asof_date=effective_asof)
        candidate_lookup_asof = pd.to_datetime(
            candidate_source_availability.get("candidate_lookup_asof_date"),
            utc=True,
            errors="coerce",
        )
        candidate_lookup_asof = None if pd.isna(candidate_lookup_asof) else candidate_lookup_asof.normalize()
        candidate_source_asof = candidate_lookup_asof if candidate_lookup_asof is not None else effective_asof
        candidates = load_candidate_rows(asof_date=candidate_source_asof, limit=limit * 2)
        rejections = load_rejection_rows(asof_date=candidate_source_asof, limit=limit * 2)
        allocations = load_allocation_rows(asof_date=effective_asof, limit=limit * 2)
        portfolio = load_portfolio_rows(asof_date=effective_asof, limit=limit * 2)
        post_advisory_activity = load_post_advisory_activity(
            cutoff_ts=_latest_decision_load_ts(rows),
            limit=min(25, int(limit)),
            asof_date=effective_asof,
        )
        context_overlay_reliability = load_context_overlay_reliability_for_diagnostics(asof_date=effective_asof)
        signal_quality_attribution = load_signal_quality_attribution_for_diagnostics(asof_date=effective_asof)
        context_overlay_source_diagnostics = load_context_overlay_source_diagnostics(asof_date=effective_asof)
        context_watchlist_intake = load_context_watchlist_intake(asof_date=effective_asof, limit=limit * 2)
        context_watch_technical_precheck = load_context_watch_technical_precheck(
            asof_date=effective_asof,
            limit=max(200, limit * 2),
        )
        context_overlay_refresh_preview = load_context_overlay_refresh_preview(
            asof_date=effective_asof,
            limit=min(50, int(limit)),
        )
        context_overlay_signal_refresh_state = load_context_overlay_signal_refresh_state(
            context_overlay_refresh_preview=context_overlay_refresh_preview,
        )
        causal_memory_refresh_preview = load_causal_memory_refresh_preview(
            asof_date=effective_asof,
            limit=min(25, int(limit)),
        )
        market_participation_context = load_market_participation_context(asof_date=effective_asof)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.recommendation_diagnostics",
            fallback_type="recommendation_diagnostics_load_failed",
            source=f"{ACTION_RECOMMENDATIONS_TABLE},{CANDIDATES_TABLE},{REJECTIONS_TABLE},{ALLOCATIONS_TABLE},{PORTFOLIO_TABLE}",
            severity="error",
            reason="Recommendation diagnostics could not load persisted recommendations, upstream candidates, or risk/portfolio handoff context.",
            error=exc,
            metadata={"asof_date": None if asof_date is None else str(asof_date), "limit": int(limit)},
        )
        raise
    return summarize_recommendation_rows(
        rows,
        asof_date=asof_date,
        candidates=candidates,
        rejections=rejections,
        allocations=allocations,
        portfolio=portfolio,
        candidate_source_availability=candidate_source_availability,
        post_advisory_activity=post_advisory_activity,
        context_overlay_reliability=context_overlay_reliability,
        signal_quality_attribution=signal_quality_attribution,
        context_overlay_source_diagnostics=context_overlay_source_diagnostics,
        context_watchlist_intake=context_watchlist_intake,
        context_watch_technical_precheck=context_watch_technical_precheck,
        context_overlay_refresh_preview=context_overlay_refresh_preview,
        context_overlay_signal_refresh_state=context_overlay_signal_refresh_state,
        causal_memory_refresh_preview=causal_memory_refresh_preview,
        market_participation_context=market_participation_context,
    )


def format_text(payload: dict[str, Any]) -> str:
    upstream = payload.get("upstream_candidates") if isinstance(payload.get("upstream_candidates"), dict) else {}
    handoff = payload.get("risk_portfolio_handoff") if isinstance(payload.get("risk_portfolio_handoff"), dict) else {}
    context_policy = payload.get("context_gate_policy") if isinstance(payload.get("context_gate_policy"), dict) else {}
    code_freshness = payload.get("code_data_freshness") if isinstance(payload.get("code_data_freshness"), dict) else {}
    post_activity = payload.get("post_advisory_activity") if isinstance(payload.get("post_advisory_activity"), dict) else {}
    stale_gate_artifacts = payload.get("stale_context_gate_artifacts") if isinstance(payload.get("stale_context_gate_artifacts"), dict) else {}
    diagnostic_trust = payload.get("diagnostic_trust") if isinstance(payload.get("diagnostic_trust"), dict) else {}
    primary_cause = payload.get("primary_no_buy_cause") if isinstance(payload.get("primary_no_buy_cause"), dict) else {}
    technical_artifacts = payload.get("technical_artifacts") if isinstance(payload.get("technical_artifacts"), dict) else {}
    manual_review_breakdown = payload.get("manual_review_breakdown") if isinstance(payload.get("manual_review_breakdown"), dict) else {}
    context_overlay_pressure = payload.get("context_overlay_pressure") if isinstance(payload.get("context_overlay_pressure"), dict) else {}
    context_overlay_sources = payload.get("context_overlay_source_diagnostics") if isinstance(payload.get("context_overlay_source_diagnostics"), dict) else {}
    context_watchlist_intake = payload.get("context_watchlist_intake") if isinstance(payload.get("context_watchlist_intake"), dict) else {}
    context_watch_technical_precheck = (
        payload.get("context_watch_technical_precheck")
        if isinstance(payload.get("context_watch_technical_precheck"), dict)
        else {}
    )
    context_watch_confirmation = (
        context_watchlist_intake.get("technical_confirmation_plan_summary")
        if isinstance(context_watchlist_intake.get("technical_confirmation_plan_summary"), dict)
        else {}
    )
    context_overlay_refresh_preview = payload.get("context_overlay_refresh_preview") if isinstance(payload.get("context_overlay_refresh_preview"), dict) else {}
    context_overlay_signal_refresh_state = payload.get("context_overlay_signal_refresh_state") if isinstance(payload.get("context_overlay_signal_refresh_state"), dict) else {}
    causal_memory_refresh_preview = payload.get("causal_memory_refresh_preview") if isinstance(payload.get("causal_memory_refresh_preview"), dict) else {}
    market_participation_context = payload.get("market_participation_context") if isinstance(payload.get("market_participation_context"), dict) else {}
    layered_attribution = (
        payload.get("layered_underparticipation_attribution")
        if isinstance(payload.get("layered_underparticipation_attribution"), dict)
        else {}
    )
    context_to_entry_pipeline = (
        payload.get("context_to_entry_pipeline")
        if isinstance(payload.get("context_to_entry_pipeline"), dict)
        else {}
    )
    source_family_attribution = (
        context_to_entry_pipeline.get("source_family_attribution")
        if isinstance(context_to_entry_pipeline.get("source_family_attribution"), dict)
        else {}
    )
    advisory_rerun_readiness = payload.get("advisory_rerun_readiness") if isinstance(payload.get("advisory_rerun_readiness"), dict) else {}
    candidate_source_availability = (
        payload.get("candidate_source_availability")
        if isinstance(payload.get("candidate_source_availability"), dict)
        else {}
    )
    context_overlay_reliability = payload.get("context_overlay_reliability") if isinstance(payload.get("context_overlay_reliability"), dict) else {}
    signal_quality_attribution = payload.get("signal_quality_attribution") if isinstance(payload.get("signal_quality_attribution"), dict) else {}
    latest_context_overlay_reliability = (
        context_overlay_reliability.get("latest_available_after_asof")
        if isinstance(context_overlay_reliability.get("latest_available_after_asof"), dict)
        else {}
    )
    market_context_blocks = payload.get("market_context_blocks") if isinstance(payload.get("market_context_blocks"), dict) else {}
    multi_context_attribution = payload.get("multi_context_attribution") if isinstance(payload.get("multi_context_attribution"), dict) else {}
    research_context_policy_alignment = (
        payload.get("research_context_policy_alignment")
        if isinstance(payload.get("research_context_policy_alignment"), dict)
        else {}
    )
    transition_policy_gates = payload.get("transition_policy_gates") if isinstance(payload.get("transition_policy_gates"), dict) else {}
    trusted_context_rule_adjustments = (
        payload.get("trusted_context_rule_adjustments")
        if isinstance(payload.get("trusted_context_rule_adjustments"), dict)
        else {}
    )
    reviewed_context_rule_eligibility = (
        payload.get("reviewed_context_rule_eligibility")
        if isinstance(payload.get("reviewed_context_rule_eligibility"), dict)
        else {}
    )
    action_queue_authority_state = (
        payload.get("action_queue_authority_state")
        if isinstance(payload.get("action_queue_authority_state"), dict)
        else {}
    )
    primary_commands = primary_cause.get("next_commands") if isinstance(primary_cause.get("next_commands"), list) else []
    primary_has_context_to_entry_commands = (
        str(primary_cause.get("next_commands_source") or "")
        == "context_to_entry_pipeline_then_advisory_rerun_readiness"
        and bool(primary_commands)
    )
    action_queue_authority_action = str(action_queue_authority_state.get("operator_action") or "")
    if primary_has_context_to_entry_commands and "all_advisory" in action_queue_authority_action:
        action_queue_authority_action = (
            "Action Queue rows are mixed or candidate freshness is unclear; use the primary context-to-entry repair command chain before policy tuning."
        )
    regime_overlay = upstream.get("regime_overlay_rejections") if isinstance(upstream.get("regime_overlay_rejections"), dict) else {}
    technical_readiness = upstream.get("technical_entry_readiness") if isinstance(upstream.get("technical_entry_readiness"), dict) else {}
    primary_evidence = primary_cause.get("evidence") if isinstance(primary_cause.get("evidence"), dict) else {}
    threshold_research_plan = (
        primary_evidence.get("technical_threshold_research_plan")
        if isinstance(primary_evidence.get("technical_threshold_research_plan"), dict)
        else {}
    )
    policy_summary = context_policy.get("policy_summary") if isinstance(context_policy.get("policy_summary"), dict) else {}
    single_regime_exposure = (
        context_policy.get("single_regime_hard_gate_exposure")
        if isinstance(context_policy.get("single_regime_hard_gate_exposure"), dict)
        else {}
    )
    action_refresh_sync = post_activity.get("action_refresh_sync") if isinstance(post_activity.get("action_refresh_sync"), dict) else {}
    recommended_path = post_activity.get("recommended_path") if isinstance(post_activity.get("recommended_path"), dict) else {}
    lines = [
        "Recommendation Diagnostics",
        f"Status: {payload.get('status')} asof={payload.get('asof_date')}",
        f"Rows: {payload.get('row_count', 0)}",
        f"Actions: {payload.get('action_counts', {})}",
        f"Positive BUY/BUY_MORE rows: {payload.get('positive_action_count', 0)}; complete broker candidates: {payload.get('broker_candidate_count', 0)}",
        f"No-buy diagnosis: {payload.get('no_buy_diagnosis')}",
        (
            "Primary no-BUY cause: "
            f"{primary_cause.get('code')} "
            f"severity={primary_cause.get('severity')} "
            f"action={primary_cause.get('operator_action')}"
        ),
        f"Suppression counts: {payload.get('suppression_counts', {})}",
        (
            "Transition policy gates: "
            f"status={transition_policy_gates.get('status', 'missing')} "
            f"rows={transition_policy_gates.get('transition_policy_gate_count', 0)} "
            f"original={transition_policy_gates.get('original_action_counts', {})} "
            f"downgraded={transition_policy_gates.get('downgraded_action_counts', {})} "
            f"blockers={transition_policy_gates.get('transition_blocker_counts', {})}"
        ),
        (
            "Trusted context-rule adjustments: "
            f"status={trusted_context_rule_adjustments.get('status', 'missing')} "
            f"rows={trusted_context_rule_adjustments.get('adjusted_row_count', 0)} "
            f"matched_rules={trusted_context_rule_adjustments.get('matched_rule_count', 0)} "
            f"adjustments={trusted_context_rule_adjustments.get('adjustment_counts', {})} "
            f"authority_violations={trusted_context_rule_adjustments.get('broker_authority_violation_count', 0)}"
        ),
        (
            "Reviewed context-rule eligibility: "
            f"status={reviewed_context_rule_eligibility.get('status', 'missing')} "
            f"rows={reviewed_context_rule_eligibility.get('row_count', 0)} "
            f"rules={reviewed_context_rule_eligibility.get('total_rule_count', 0)} "
            f"eligible={reviewed_context_rule_eligibility.get('eligible_rule_count', 0)} "
            f"ineligible={reviewed_context_rule_eligibility.get('ineligible_rule_count', 0)} "
            f"unknown={reviewed_context_rule_eligibility.get('unknown_eligibility_rule_count', 0)}"
        ),
        (
            "Research context-policy alignment: "
            f"status={research_context_policy_alignment.get('status', 'missing')} "
            f"single_regime_flags={research_context_policy_alignment.get('active_single_regime_flags', [])} "
            f"context_dims={research_context_policy_alignment.get('active_multi_context_dimensions', [])} "
            f"runtime_families={research_context_policy_alignment.get('runtime_usable_context_family_count', 0)} "
            f"attribution_blockers={research_context_policy_alignment.get('signal_quality_attribution_blocked_count', 0)} "
            f"eligible_rules={research_context_policy_alignment.get('runtime_eligible_reviewed_context_rule_count', 0)} "
            f"blockers={research_context_policy_alignment.get('blockers', [])}"
        ),
        (
            "Upstream summary: "
            f"candidates={upstream.get('candidate_rows', 0)} "
            f"rejections={upstream.get('rejection_rows', 0)} "
            f"constructive={upstream.get('positive_or_constructive_candidate_count', 0)} "
            f"pass_now_with_technical_ignore={upstream.get('pass_now_with_technical_ignore_count', 0)}"
        ),
        (
            "Candidate source availability: "
            f"status={candidate_source_availability.get('status')} "
            f"requested={candidate_source_availability.get('requested_asof_date')} "
            f"lookup={candidate_source_availability.get('candidate_lookup_asof_date')} "
            f"rows_for_asof={candidate_source_availability.get('candidate_rows_for_asof', 0)} "
            f"latest={candidate_source_availability.get('latest_candidate_asof_date')} "
            f"latest_rows={candidate_source_availability.get('latest_candidate_rows', 0)} "
            f"latest_trading={candidate_source_availability.get('latest_trading_asof_date')} "
            f"matches_latest_trading={candidate_source_availability.get('latest_candidate_matches_latest_trading_day')} "
            f"trading_age_days={candidate_source_availability.get('latest_candidate_trading_age_days')} "
            f"date_gap_days={candidate_source_availability.get('date_gap_days')} "
            f"relation={candidate_source_availability.get('latest_candidate_relation')} "
            f"age_days={candidate_source_availability.get('latest_candidate_age_days')}"
        ),
        f"Candidate states: {upstream.get('candidate_state_counts', {})}",
        f"Technical states: {upstream.get('technical_state_counts', {})}",
        (
            "Candidate survival: "
            f"diagnosis={upstream.get('candidate_survival_diagnosis')} "
            f"top={_format_candidate_survival_matrix(upstream.get('candidate_survival_matrix'))}"
        ),
        (
            "Technical entry readiness: "
            f"diagnosis={technical_readiness.get('diagnosis')} "
            f"confirmed={technical_readiness.get('confirmed_entry_candidate_count', 0)} "
            f"watch_or_near={technical_readiness.get('watch_or_near_entry_candidate_count', 0)} "
            f"ignore_or_reject={technical_readiness.get('ignore_or_reject_candidate_count', 0)} "
            f"top_soft_failures={technical_readiness.get('top_soft_failures', [])[:5]} "
            f"trigger_blockers={_format_trigger_blockers(technical_readiness.get('technical_trigger_blockers'))} "
            f"buy_readiness={_format_buy_readiness_blockers(technical_readiness.get('buy_readiness_blockers'))}"
        ),
        f"Technical archetype readiness: {_format_archetype_readiness(technical_readiness.get('archetype_readiness'))}",
        (
            "Technical artifacts: "
            f"status={technical_artifacts.get('status')} "
            f"pass_now_ignore={technical_artifacts.get('pass_now_with_technical_ignore_count', 0)} "
            f"source_precedes_code={bool(technical_artifacts.get('source_rows_precede_current_code'))} "
            f"rerun_required={bool(technical_artifacts.get('rerun_required_before_policy_tuning'))}"
        ),
        f"Rejection reasons top: {upstream.get('rejection_reason_counts', {})}",
        (
            "Regime/overlay gates: "
            f"hard={regime_overlay.get('hard_context_gate_count', 0)} "
            f"context_only={regime_overlay.get('context_only_warning_count', 0)} "
            f"counts={regime_overlay.get('counts', {})}"
        ),
        (
            "Context-overlay pressure: "
            f"status={context_overlay_pressure.get('status')} "
            f"positive_or_watch={context_overlay_pressure.get('positive_or_watch_pressure_count', 0)} "
            f"negative={context_overlay_pressure.get('negative_pressure_count', 0)} "
            f"suppressed={context_overlay_pressure.get('suppressed_context_count', 0)} "
            f"row_actions={context_overlay_pressure.get('row_action_counts', {})} "
            f"families={context_overlay_pressure.get('context_source_family_counts', {})}"
        ),
        (
            "Context source-family attribution: "
            f"status={source_family_attribution.get('status', 'missing')} "
            f"families={source_family_attribution.get('family_count', 0)} "
            f"blockers={source_family_attribution.get('blocker_counts', {})} "
            f"top_blocked={_format_context_source_family_blockers(source_family_attribution)} "
            f"commands={_format_context_source_family_commands(source_family_attribution)}"
        ),
        (
            "Context-overlay sources: "
            f"status={context_overlay_sources.get('status')} "
            f"source_rows={context_overlay_sources.get('source_rows_total', 0)} "
            f"active_authorized={context_overlay_sources.get('active_authorized_rows_total', 0)} "
            f"available_sources={context_overlay_sources.get('available_source_count', 0)} "
            f"empty_reason={context_overlay_sources.get('empty_reason')}"
        ),
        (
            "Context watchlist intake: "
            f"status={context_watchlist_intake.get('status')} "
            f"current={context_watchlist_intake.get('current_for_reconcile')} "
            f"freshness={context_watchlist_intake.get('context_watchlist_freshness_status')} "
            f"counts_scope={context_watchlist_intake.get('counts_scope')} "
            f"rows={context_watchlist_intake.get('row_count', 0)} "
            f"sample={context_watchlist_intake.get('sample_row_count', 0)}/{context_watchlist_intake.get('sample_limit')} "
            f"active={context_watchlist_intake.get('active_watch_count', 0)} "
            f"breakout={context_watchlist_intake.get('breakout_watch_count', 0)} "
            f"suppressed={context_watchlist_intake.get('suppressed_count', 0)} "
            f"sync_asof_match={context_watchlist_intake.get('sync_asof_matches')} "
            f"fresh_for_signal={context_watchlist_intake.get('watchlist_fresh_for_signal_refresh')} "
            f"policy_effects={context_watchlist_intake.get('policy_effect_counts', {})}"
        ),
        (
            "Context watch confirmation: "
            f"plans={context_watch_confirmation.get('plan_count', 0)} "
            f"active_with_plan={context_watchlist_intake.get('active_confirmation_plan_count', 0)} "
            f"active_missing_plan={context_watchlist_intake.get('active_missing_confirmation_plan_count', 0)} "
            f"entry_gates={context_watch_confirmation.get('entry_gate_counts', {})} "
            f"wait_for={context_watch_confirmation.get('wait_for_counts', {})}"
        ),
        (
            "Context watch technical precheck: "
            f"status={context_watch_technical_precheck.get('status')} "
            f"active={context_watch_technical_precheck.get('active_context_watch_symbol_count', 0)} "
            f"covered={context_watch_technical_precheck.get('technical_symbol_coverage_count', 0)} "
            f"missing={context_watch_technical_precheck.get('missing_technical_symbol_count', 0)} "
            f"dhan_identity={context_watch_technical_precheck.get('dhan_identity_status')} "
            f"dhan_missing={context_watch_technical_precheck.get('missing_dhan_identity_count', 0)} "
            f"refreshable={context_watch_technical_precheck.get('technical_refresh_symbol_count', 0)} "
            f"refresh_issues={context_watch_technical_precheck.get('latest_refresh_issue_symbol_count', 0)} "
            f"newer_refresh_issues={context_watch_technical_precheck.get('newer_refresh_attempt_issue_symbol_count', 0)} "
            f"hard_input_blockers={(context_watch_technical_precheck.get('technical_input_blockers') or {}).get('hard_blocker_symbol_count') if isinstance(context_watch_technical_precheck.get('technical_input_blockers'), dict) else 0} "
            f"stale_history_blockers={(context_watch_technical_precheck.get('technical_input_blockers') or {}).get('stale_history_symbol_count') if isinstance(context_watch_technical_precheck.get('technical_input_blockers'), dict) else 0} "
            f"latest_trading={context_watch_technical_precheck.get('latest_trading_day')} "
            f"latest_dhan_daily={context_watch_technical_precheck.get('latest_dhan_daily_date')} "
            f"technical_command_date={context_watch_technical_precheck.get('technical_command_date')} "
            f"latest_technical={context_watch_technical_precheck.get('latest_technical_asof_date')} "
            f"stale_days={context_watch_technical_precheck.get('technical_stale_days')} "
            f"constructive={context_watch_technical_precheck.get('constructive_precheck_count', 0)} "
            f"breakout_like={context_watch_technical_precheck.get('breakout_like_precheck_count', 0)}"
        ),
        (
            "Context-overlay refresh preview: "
            f"status={context_overlay_refresh_preview.get('status')} "
            f"target_rows={context_overlay_refresh_preview.get('target_rows', 0)} "
            f"signal_rows={context_overlay_refresh_preview.get('signal_rows', 0)} "
            f"macro_targets={context_overlay_refresh_preview.get('macro_context_target_rows', 0)} "
            f"positive_watch_targets={context_overlay_refresh_preview.get('positive_watch_target_rows', 0)} "
            f"affected={context_overlay_refresh_preview.get('affected_symbol_count', 0)} "
            f"command={context_overlay_refresh_preview.get('recommended_command')}"
        ),
        (
            "Context-overlay signal-refresh state: "
            f"status={context_overlay_signal_refresh_state.get('status')} "
            f"current={context_overlay_signal_refresh_state.get('current_for_preview')} "
            f"state_signals={context_overlay_signal_refresh_state.get('state_signal_rows', 0)} "
            f"preview_signals={context_overlay_signal_refresh_state.get('preview_signal_rows', 0)} "
            f"asof_matches={context_overlay_signal_refresh_state.get('asof_matches')}"
        ),
        (
            "Causal-memory refresh preview: "
            f"status={causal_memory_refresh_preview.get('status')} "
            f"target_rows={causal_memory_refresh_preview.get('target_rows', 0)} "
            f"signal_rows={causal_memory_refresh_preview.get('signal_rows', 0)} "
            f"affected={causal_memory_refresh_preview.get('affected_symbol_count', 0)} "
            f"min_score={causal_memory_refresh_preview.get('min_decayed_pressure_score')} "
            f"lookback_days={causal_memory_refresh_preview.get('lookback_days')} "
            f"command={causal_memory_refresh_preview.get('recommended_command')}"
        ),
        (
            "Context-overlay reliability: "
            f"status={context_overlay_reliability.get('status')} "
            f"evaluated_at={context_overlay_reliability.get('evaluated_at')} "
            f"families={context_overlay_reliability.get('family_count', 0)} "
            f"usable={context_overlay_reliability.get('runtime_usable_family_count', 0)} "
            f"classifications={context_overlay_reliability.get('classification_counts', {})} "
            f"latest_after_asof_status={latest_context_overlay_reliability.get('status')} "
            f"latest_after_asof_evaluated_at={latest_context_overlay_reliability.get('evaluated_at')}"
        ),
        (
            "Signal-quality attribution: "
            f"status={signal_quality_attribution.get('status')} "
            f"evaluated_at={signal_quality_attribution.get('evaluated_at')} "
            f"rows={signal_quality_attribution.get('row_count', 0)} "
            f"beta_only={signal_quality_attribution.get('benchmark_beta_not_overlay_alpha_count', 0)} "
            f"needs_benchmark={signal_quality_attribution.get('needs_benchmark_attribution_count', 0)} "
            f"blocked={signal_quality_attribution.get('benchmark_or_attribution_blocked_count', 0)}"
        ),
        (
            "Market participation: "
            f"status={market_participation_context.get('status')} "
            f"label={market_participation_context.get('market_participation_label')} "
            f"benchmark={market_participation_context.get('benchmark_name')} "
            f"ret20={market_participation_context.get('benchmark_ret_20d')} "
            f"ret60={market_participation_context.get('benchmark_ret_60d')} "
            f"regime={market_participation_context.get('regime_name')} "
            f"macro_risk={market_participation_context.get('macro_risk_state')}"
        ),
        (
            "Multi-context attribution: "
            f"status={multi_context_attribution.get('status')} "
            f"active_dimensions={multi_context_attribution.get('active_dimensions', [])} "
            f"suppressed={multi_context_attribution.get('suppressed_dimension_counts', {})} "
            f"missing={multi_context_attribution.get('missing_label_counts', {})} "
            f"breadth={multi_context_attribution.get('label_counts', {}).get('breadth', {}) if isinstance(multi_context_attribution.get('label_counts'), dict) else {}} "
            f"macro={multi_context_attribution.get('label_counts', {}).get('macro_stress', {}) if isinstance(multi_context_attribution.get('label_counts'), dict) else {}} "
            f"sector_symbol={multi_context_attribution.get('label_counts', {}).get('sector_symbol', {}) if isinstance(multi_context_attribution.get('label_counts'), dict) else {}} "
            f"technical={multi_context_attribution.get('label_counts', {}).get('technical', {}) if isinstance(multi_context_attribution.get('label_counts'), dict) else {}} "
            f"overlay={multi_context_attribution.get('label_counts', {}).get('context_overlay', {}) if isinstance(multi_context_attribution.get('label_counts'), dict) else {}}"
        ),
        (
            "Layered underparticipation attribution: "
            f"status={layered_attribution.get('status')} "
            f"severity={layered_attribution.get('severity')} "
            f"benchmark={layered_attribution.get('benchmark_participation_label')} "
            f"constructive={layered_attribution.get('constructive_candidate_count', 0)} "
            f"confirmed={layered_attribution.get('confirmed_entry_candidate_count', 0)} "
            f"context_sources={layered_attribution.get('active_context_overlay_source_rows', 0)} "
            f"context_signals={layered_attribution.get('context_overlay_preview_signal_rows', 0)} "
            f"action={layered_attribution.get('operator_action')}"
        ),
        (
            "Context-to-entry pipeline: "
            f"status={context_to_entry_pipeline.get('status')} "
            f"bottleneck={context_to_entry_pipeline.get('bottleneck')} "
            f"sources={context_to_entry_pipeline.get('active_authorized_context_rows', 0)} "
            f"watchlist={context_to_entry_pipeline.get('active_context_watchlist_rows', 0)} "
            f"breakout_watchlist={context_to_entry_pipeline.get('breakout_context_watchlist_rows', 0)} "
            f"signals={context_to_entry_pipeline.get('refresh_signal_rows', 0)} "
            f"pressure={context_to_entry_pipeline.get('positive_or_watch_context_pressure_count', 0)} "
            f"constructive={context_to_entry_pipeline.get('constructive_candidate_count', 0)} "
            f"confirmed={context_to_entry_pipeline.get('confirmed_entry_candidate_count', 0)} "
            f"final_positive={context_to_entry_pipeline.get('final_positive_action_count', 0)} "
            f"action={context_to_entry_pipeline.get('operator_action')}"
        ),
        (
            "Market-context blocks: "
            f"status={market_context_blocks.get('status')} "
            f"blocked={market_context_blocks.get('blocked_count', 0)} "
            f"reasons={market_context_blocks.get('reason_counts', {})} "
            f"macro_states={market_context_blocks.get('macro_risk_state_counts', {})} "
            f"blocked_labels={market_context_blocks.get('blocked_label_counts', {})}"
        ),
        (
            "Manual Review breakdown: "
            f"status={manual_review_breakdown.get('status')} "
            f"total={manual_review_breakdown.get('total_manual_review_rows', 0)} "
            f"buckets={manual_review_breakdown.get('bucket_counts', {})} "
            f"automation_targets={manual_review_breakdown.get('automation_targets', {})}"
        ),
        (
            "Risk/portfolio handoff: "
            f"allocations={handoff.get('allocation_rows', 0)} "
            f"portfolio_rows={handoff.get('portfolio_rows', 0)} "
            f"risk_unconfirmed={handoff.get('risk_base_unconfirmed_count', 0)} "
            f"portfolio_unconfirmed={handoff.get('portfolio_technical_entry_not_confirmed_count', 0)}"
        ),
        f"Context gate policy: {policy_summary}",
        (
            "Single-regime hard-gate exposure: "
            f"status={single_regime_exposure.get('status')} "
            f"active_single_regime_flags={single_regime_exposure.get('active_single_regime_flags', [])} "
            f"active_context_hard_flags={single_regime_exposure.get('active_context_hard_flags', [])}"
        ),
        (
            "Code/data freshness: "
            f"status={code_freshness.get('status')} "
            f"source={code_freshness.get('latest_source_load_ts')} "
            f"code={code_freshness.get('latest_code_mtime')}"
        ),
        (
            "Post-advisory watcher activity: "
            f"status={post_activity.get('status')} "
            f"new_rows={post_activity.get('total_new_rows', 0)} "
            f"action_refresh_candidates={post_activity.get('action_refresh_candidate_count', 0)} "
            f"action_refresh_counts={post_activity.get('action_refresh_candidate_counts', {})} "
            f"action_refresh_sync={action_refresh_sync.get('status')} "
            f"action_refresh_recommended={bool(post_activity.get('action_refresh_recommended'))} "
            f"full_advisory_required={post_activity.get('full_advisory_required_count', 0)} "
            f"rerun_recommended={bool(post_activity.get('full_advisory_rerun_recommended'))} "
            f"path={recommended_path.get('code')}"
        ),
        f"Diagnostic trust: status={diagnostic_trust.get('status')} reasons={diagnostic_trust.get('reasons', [])}",
        (
            "Stale context-gate conflict: "
            f"{bool(diagnostic_trust.get('stale_context_gate_conflict'))} "
            f"hard_context_gates={diagnostic_trust.get('hard_context_gate_count', 0)} "
            f"stale_hard={diagnostic_trust.get('stale_hard_context_gate_count', 0)} "
            f"active_hard={diagnostic_trust.get('active_hard_context_gate_count', 0)} "
            f"stale_by_type={diagnostic_trust.get('stale_hard_context_gate_counts_by_type', {})} "
            f"current_rule_hard_context={bool(diagnostic_trust.get('rule_context_hard_enabled'))}"
        ),
        (
            "Stale hard-gate artifacts: "
            f"status={stale_gate_artifacts.get('status')} "
            f"stale={stale_gate_artifacts.get('stale_artifact_count', 0)} "
            f"active={stale_gate_artifacts.get('active_hard_gate_count', 0)}"
        ),
        "",
        "Next steps:",
    ]
    if action_queue_authority_state.get("status"):
        lines.insert(
            8,
            (
                "Action Queue authority: "
                f"status={action_queue_authority_state.get('status')} "
                f"level={action_queue_authority_state.get('authority_level')} "
                f"sources={action_queue_authority_state.get('action_source_counts', {})} "
                f"candidate_status={action_queue_authority_state.get('candidate_source_status')} "
                f"candidate_rows={action_queue_authority_state.get('candidate_rows_for_asof', 0)} "
                f"latest_candidate={action_queue_authority_state.get('latest_candidate_asof_date')} "
                f"full_advisory_required={action_queue_authority_state.get('full_advisory_required')} "
                f"action={action_queue_authority_action}"
            ),
        )
    if advisory_rerun_readiness:
        lines.insert(
            -2,
            (
                "Advisory rerun readiness: "
                f"status={advisory_rerun_readiness.get('status')} "
                f"blockers={advisory_rerun_readiness.get('blockers', [])} "
                f"candidate_status={advisory_rerun_readiness.get('candidate_source_status')} "
                f"feature_blocked={advisory_rerun_readiness.get('feature_freshness_blocked_count', 0)} "
                f"context_source={advisory_rerun_readiness.get('context_overlay_source_status')} "
                f"context_signals={advisory_rerun_readiness.get('context_overlay_preview_signal_rows', 0)} "
                f"context_positive_watch={advisory_rerun_readiness.get('context_overlay_positive_watch_target_rows', 0)} "
                f"memory_signals={advisory_rerun_readiness.get('causal_memory_preview_signal_rows', 0)} "
                f"action_refresh_pending={advisory_rerun_readiness.get('post_advisory_action_refresh_pending')}"
            ),
        )
    if threshold_research_plan:
        lines.insert(
            8,
            (
                "Technical threshold research plan: "
                f"status={threshold_research_plan.get('status', 'not_applicable')} "
                f"ready={threshold_research_plan.get('ready_for_calibration')} "
                f"asof={threshold_research_plan.get('candidate_asof_date')} "
                f"trigger_contract={threshold_research_plan.get('trigger_blocker_status')} "
                f"buy_readiness_contract={threshold_research_plan.get('buy_readiness_status')} "
                f"reconstructed_contracts={threshold_research_plan.get('diagnostic_reconstructed_contract_count', 0)} "
                f"near_miss_candidates={threshold_research_plan.get('trigger_near_miss_candidate_count', 0)} "
                f"near_miss_do_not_relax={threshold_research_plan.get('trigger_near_miss_do_not_relax_count', 0)} "
                f"near_miss_needs_more_labels={threshold_research_plan.get('trigger_near_miss_needs_more_label_count', 0)} "
                f"regenerate_before_promotion={threshold_research_plan.get('candidate_regeneration_recommended_before_promotion', False)} "
                f"note={threshold_research_plan.get('label_maturity_note')}"
            ),
        )
    lines.extend([f"- {item}" for item in payload.get("operator_next_steps") or []])
    next_commands = primary_cause.get("next_commands") if isinstance(primary_cause.get("next_commands"), list) else []
    if next_commands:
        lines.extend(["", "Recommended commands:"])
        for item in next_commands[:5]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('command')}: {item.get('purpose')}")
            else:
                lines.append(f"- {item}")
    pipeline_commands = (
        context_to_entry_pipeline.get("next_commands")
        if isinstance(context_to_entry_pipeline.get("next_commands"), list)
        else []
    )
    if pipeline_commands:
        lines.extend(["", "Context-to-entry commands:"])
        for item in pipeline_commands[:3]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('command')}: {item.get('purpose')}")
            else:
                lines.append(f"- {item}")
    readiness_commands = (
        advisory_rerun_readiness.get("recommended_commands")
        if isinstance(advisory_rerun_readiness.get("recommended_commands"), list)
        else []
    )
    if readiness_commands:
        lines.extend(["", "Advisory rerun readiness commands:"])
        for item in readiness_commands[:5]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('command')}: {item.get('purpose')}")
            else:
                lines.append(f"- {item}")
    manual_commands = manual_review_breakdown.get("recommended_commands") if isinstance(manual_review_breakdown.get("recommended_commands"), list) else []
    if manual_commands:
        lines.extend(["", "Manual Review automation commands:"])
        for item in manual_commands[:5]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('command')}: {item.get('purpose')}")
            else:
                lines.append(f"- {item}")
    context_reliability_commands = (
        context_overlay_reliability.get("recommended_commands")
        if isinstance(context_overlay_reliability.get("recommended_commands"), list)
        else []
    )
    if context_reliability_commands:
        lines.extend(["", "Context-overlay reliability commands:"])
        for item in context_reliability_commands[:5]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('command')}: {item.get('purpose')}")
            else:
                lines.append(f"- {item}")
    signal_quality_commands = (
        signal_quality_attribution.get("recommended_commands")
        if isinstance(signal_quality_attribution.get("recommended_commands"), list)
        else []
    )
    if signal_quality_commands:
        lines.extend(["", "Signal-quality attribution commands:"])
        for item in signal_quality_commands[:5]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('command')}: {item.get('purpose')}")
            else:
                lines.append(f"- {item}")
    multi_context_commands = (
        multi_context_attribution.get("recommended_commands")
        if isinstance(multi_context_attribution.get("recommended_commands"), list)
        else []
    )
    if multi_context_commands:
        lines.extend(["", "Multi-context metadata repair commands:"])
        payload_asof = pd.to_datetime(payload.get("asof_date"), utc=True, errors="coerce")
        payload_asof_text = None if pd.isna(payload_asof) else payload_asof.date().isoformat()
        for item in multi_context_commands[:3]:
            if isinstance(item, dict):
                command = str(item.get("command") or "")
                if payload_asof_text:
                    command = command.replace("YYYY-MM-DD", payload_asof_text)
                lines.append(f"- {command}: {item.get('purpose')}")
            else:
                command = str(item)
                if payload_asof_text:
                    command = command.replace("YYYY-MM-DD", payload_asof_text)
                lines.append(f"- {command}")
    if post_activity.get("action_refresh_recommended") and post_activity.get("action_refresh_command"):
        lines.append(
            f"Fast action-refresh command: {post_activity.get('action_refresh_command')} "
            "(review-only Action Queue refresh; full advisory remains authoritative)."
        )
    constructive = upstream.get("top_positive_or_constructive_candidates") if isinstance(upstream.get("top_positive_or_constructive_candidates"), list) else []
    if constructive:
        lines.extend(["", "Top upstream constructive candidates:"])
        for item in constructive[:5]:
            lines.append(
                f"- {item.get('symbol')}: {item.get('candidate_state')} / technical={item.get('technical_state')} "
                f"setup={item.get('setup_id')} final={item.get('final_action')}"
            )
    gate_samples = regime_overlay.get("samples") if isinstance(regime_overlay.get("samples"), list) else []
    if gate_samples:
        lines.extend(["", "Top regime/overlay gate samples:"])
        for item in gate_samples[:5]:
            lines.append(
                f"- {item.get('symbol')}: {item.get('bucket')} {item.get('reason_code')} "
                f"setup={item.get('setup_id')} detail={item.get('reason_detail')}"
            )
    stale_samples = stale_gate_artifacts.get("samples") if isinstance(stale_gate_artifacts.get("samples"), list) else []
    if stale_samples:
        lines.extend(["", "Stale hard-gate artifact samples:"])
        for item in stale_samples[:5]:
            lines.append(
                f"- {item.get('symbol')}: {item.get('bucket')} was hard when persisted, "
                f"current_policy={item.get('current_policy_effect')} setup={item.get('setup_id')}"
            )
    samples = payload.get("top_blocked_or_review_symbols") or []
    if samples:
        lines.extend(["", "Top blocked/review symbols:"])
        for item in samples[:10]:
            lines.append(
                f"- {item.get('symbol')}: {item.get('action_code')} from {item.get('action_source')}; "
                f"reasons={item.get('suppression_reasons')}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explain why persisted advisory recommendations are BUY, watch-only, or blocked.")
    parser.add_argument("--asof-date", help="Recommendation date to inspect. Defaults to latest persisted action date.")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--format", choices=["json", "text"], default="json")
    parser.add_argument(
        "--context-watch-technical-command-only",
        action="store_true",
        help=(
            "Print only the targeted technical refresh command for active CONTEXT_OVERLAY_WATCH symbols. "
            "This is read-only and intended for bounded context-to-entry repair scripts."
        ),
    )
    args = parser.parse_args(argv)
    if args.context_watch_technical_command_only:
        precheck = load_context_watch_technical_precheck(
            asof_date=parse_datetime_arg(args.asof_date) if args.asof_date else None,
            limit=max(200, int(args.limit)),
        )
        symbols = [
            str(symbol or "").strip().upper()
            for symbol in (precheck.get("targeted_repair_symbols") or [])
            if str(symbol or "").strip()
        ]
        command_date = pd.to_datetime(precheck.get("technical_command_date"), utc=True, errors="coerce")
        if symbols and not pd.isna(command_date):
            date_text = command_date.date().isoformat()
            print(
                shlex.join(
                    [
                        "python",
                        "-m",
                        "advisory.technical_features",
                        "--targeted-context-refresh",
                        "--from-date",
                        date_text,
                        "--to-date",
                        date_text,
                        "--symbols",
                        *symbols,
                    ]
                )
            )
        return 0
    payload = build_recommendation_diagnostics(
        asof_date=parse_datetime_arg(args.asof_date) if args.asof_date else None,
        limit=args.limit,
    )
    if args.format == "text":
        print(format_text(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
