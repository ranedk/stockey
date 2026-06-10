from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from utils.db import sql_to_df


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class FeatureInputSpec:
    key: str
    label: str
    table: str
    date_column: str
    symbol_column: str | None = "symbol"
    max_age_days: int = 5
    required: bool = False
    scope: str = "symbol"
    purpose: str = ""


FEATURE_INPUT_SPECS: tuple[FeatureInputSpec, ...] = (
    FeatureInputSpec("daily_ohlcv", "Daily OHLCV", "dhan_ohlcv_daily", "date", symbol_column="ticker", max_age_days=5, required=True, purpose="Price, trend, stop/target, P&L, and liquidity context."),
    FeatureInputSpec("technical_daily", "Technical Features", "advisory_technical_daily", "asof_date", max_age_days=5, required=True, purpose="Swing state, trigger, trend, participation, and risk scoring."),
    FeatureInputSpec("intraday_features", "Intraday Features", "advisory_intraday_features_daily", "asof_date", max_age_days=3, purpose="Fresh volume/price participation context for watcher and rule preflight."),
    FeatureInputSpec("macro_features", "Macro Features", "advisory_macro_features_daily", "asof_date", symbol_column=None, max_age_days=14, scope="global", purpose="Broad macro/regime context."),
    FeatureInputSpec("market_context", "Market Context", "advisory_market_context_summary_daily", "asof_date", symbol_column=None, max_age_days=7, scope="global", purpose="Breadth/risk-off gate for positive actions."),
    FeatureInputSpec("exchange_features", "Exchange Features", "advisory_exchange_features_daily", "asof_date", max_age_days=10, purpose="Corporate actions, deals, insider, short-selling, and exchange event features."),
    FeatureInputSpec("bhavcopy_evidence", "Bhavcopy Evidence", "advisory_bhavcopy_evidence_daily", "asof_date", max_age_days=10, purpose="Liquidity, circuit, participation, deal, short, and margin evidence."),
    FeatureInputSpec("announcement_evidence", "Announcement Evidence", "advisory_announcement_evidence", "published_on", max_age_days=30, purpose="Compact corporate announcement evidence available to LLM/event policy."),
    FeatureInputSpec("company_memory", "Company Memory Review", "advisory_company_memory_reviews", "review_date", max_age_days=14, purpose="LLM/deterministic company-memory review input; never final execution authority."),
)


def _safe_identifier(value: str) -> str:
    text = str(value or "").strip()
    if not IDENTIFIER_RE.match(text):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return text


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
        retries=2,
        statement_timeout_ms=5000,
    )
    return not df.empty


def table_columns(table_name: str) -> set[str]:
    df = sql_to_df(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        """,
        params=(table_name,),
        retries=2,
        statement_timeout_ms=5000,
    )
    return {str(value) for value in df.get("column_name", pd.Series(dtype=object)).dropna().tolist()}


def _status_for_age(latest_at: pd.Timestamp | None, *, asof: pd.Timestamp, max_age_days: int) -> tuple[str, float | None]:
    if latest_at is None or pd.isna(latest_at):
        return "missing", None
    latest = pd.to_datetime(latest_at, utc=True, errors="coerce")
    if pd.isna(latest):
        return "missing", None
    age_days = max(0.0, (asof - latest).total_seconds() / 86400.0)
    return ("fresh" if age_days <= float(max_age_days) else "stale"), age_days


def _latest_row_for_spec(spec: FeatureInputSpec, *, symbol: str, asof: pd.Timestamp) -> dict[str, Any]:
    table = _safe_identifier(spec.table)
    date_column = _safe_identifier(spec.date_column)
    columns = table_columns(table)
    if spec.date_column not in columns:
        return {
            "input_key": spec.key,
            "status": "missing",
            "reason": "date_column_missing",
            "table": spec.table,
            "date_column": spec.date_column,
        }
    where = [f'"{date_column}" <= %(cutoff)s']
    params: dict[str, Any] = {"cutoff": asof + pd.Timedelta(days=1)}
    symbol_column = spec.symbol_column
    if symbol_column:
        if symbol_column not in columns:
            return {
                "input_key": spec.key,
                "status": "missing",
                "reason": "symbol_column_missing",
                "table": spec.table,
                "symbol_column": symbol_column,
            }
        safe_symbol_col = _safe_identifier(symbol_column)
        where.append(f"UPPER(TRIM(COALESCE(\"{safe_symbol_col}\"::text, ''))) = %(symbol)s")
        params["symbol"] = symbol.upper()
    where_sql = " AND ".join(where)
    df = sql_to_df(
        f"""
        SELECT MAX("{date_column}") AS latest_at, COUNT(*) AS row_count
        FROM "{table}"
        WHERE {where_sql}
        """,
        params=params,
        retries=2,
        statement_timeout_ms=5000,
    )
    if df.empty:
        return {"latest_at": None, "row_count": 0}
    row = df.iloc[0]
    return {"latest_at": row.get("latest_at"), "row_count": int(row.get("row_count") or 0)}


def evaluate_feature_input(spec: FeatureInputSpec, *, symbol: str, asof_date: Any) -> dict[str, Any]:
    asof = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(asof):
        asof = pd.Timestamp.utcnow()
    normalized_symbol = str(symbol or "").strip().upper()
    base = {
        "input_key": spec.key,
        "label": spec.label,
        "table": spec.table,
        "date_column": spec.date_column,
        "symbol_column": spec.symbol_column,
        "scope": spec.scope,
        "required": bool(spec.required),
        "max_age_days": int(spec.max_age_days),
        "purpose": spec.purpose,
    }
    if not normalized_symbol and spec.symbol_column:
        return {**base, "status": "missing", "reason": "symbol_missing", "latest_at": None, "row_count": 0}
    try:
        if not table_exists(spec.table):
            return {
                **base,
                "status": "missing" if spec.required else "intentionally_skipped",
                "reason": "table_missing_required" if spec.required else "optional_table_missing",
                "latest_at": None,
                "row_count": 0,
            }
        latest = _latest_row_for_spec(spec, symbol=normalized_symbol, asof=asof)
    except Exception as exc:
        return {**base, "status": "error", "reason": f"{type(exc).__name__}: {exc}", "latest_at": None, "row_count": 0}
    if latest.get("status") in {"missing", "error"}:
        return {**base, **latest, "required": bool(spec.required)}
    row_count = int(latest.get("row_count") or 0)
    if row_count <= 0:
        return {**base, "status": "missing" if spec.required else "intentionally_skipped", "reason": "no_point_in_time_rows", "latest_at": None, "row_count": 0}
    latest_at = pd.to_datetime(latest.get("latest_at"), utc=True, errors="coerce")
    status, age_days = _status_for_age(latest_at, asof=asof, max_age_days=spec.max_age_days)
    return {
        **base,
        "status": status,
        "reason": "within_freshness_window" if status == "fresh" else "older_than_freshness_window",
        "latest_at": None if pd.isna(latest_at) else latest_at.isoformat(),
        "age_days": age_days,
        "row_count": row_count,
    }


def build_feature_freshness_contract(symbol: str, *, asof_date: Any = None) -> dict[str, Any]:
    asof = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(asof):
        asof = pd.Timestamp.utcnow()
    inputs = [evaluate_feature_input(spec, symbol=symbol, asof_date=asof) for spec in FEATURE_INPUT_SPECS]
    counts: dict[str, int] = {}
    blockers: list[dict[str, Any]] = []
    for row in inputs:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        if row.get("required") and status in {"missing", "stale", "error"}:
            blockers.append(row)
    overall = "blocked" if blockers else ("warning" if counts.get("stale") or counts.get("missing") or counts.get("error") else "ok")
    return {
        "symbol": str(symbol or "").strip().upper(),
        "asof_date": asof.isoformat(),
        "status": overall,
        "counts": counts,
        "blockers": blockers,
        "inputs": inputs,
        "notes": [
            "Required inputs currently gate confidence; optional missing inputs are shown as intentionally_skipped.",
            "This contract is read-only and explains data availability. It does not change action authority by itself.",
        ],
    }


def build_required_feature_freshness_summaries(symbols: list[str], *, asof_date: Any = None) -> dict[str, dict[str, Any]]:
    normalized = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    if not normalized:
        return {}
    summaries: dict[str, dict[str, Any]] = {
        symbol: {"status": "ok", "counts": {}, "blockers": [], "required_inputs": []}
        for symbol in normalized
    }
    for symbol in normalized:
        for spec in FEATURE_INPUT_SPECS:
            if not spec.required:
                continue
            row = evaluate_feature_input(spec, symbol=symbol, asof_date=asof_date)
            status = str(row.get("status") or "unknown")
            summary = summaries[symbol]
            counts = summary["counts"]
            counts[status] = counts.get(status, 0) + 1
            compact = {
                "input_key": row.get("input_key"),
                "label": row.get("label"),
                "status": status,
                "reason": row.get("reason"),
                "latest_at": row.get("latest_at"),
                "age_days": row.get("age_days"),
                "required": True,
            }
            summary["required_inputs"].append(compact)
            if status in {"missing", "stale", "error"}:
                summary["blockers"].append(compact)
    for symbol, summary in summaries.items():
        summary["status"] = "blocked" if summary["blockers"] else "ok"
        summary["symbol"] = symbol
    return summaries
