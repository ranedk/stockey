from __future__ import annotations

import argparse
import json
import logging
from typing import Any

import pandas as pd
from environs import Env
from pydantic import BaseModel, Field

from advisory.decision_trace import ACTION_CONFLICT_RULES_TABLE
from advisory.decision_trace import append_trace, append_trace_step, build_action_conflicts, persist_action_conflicts, safe_trace_call
from advisory.hypothesis_engine import ACTION_PLANS_TABLE, HYPOTHESES_TABLE, ensure_tables as ensure_hypothesis_tables
from advisory.market_context import load_latest_market_context
from advisory.portfolio_engine import PORTFOLIO_TABLE
from advisory.position_lifecycle import LIFECYCLE_TABLE, REBALANCE_TABLE
from advisory.prompt_registry import prompt_version as registry_prompt_version
from advisory.prompt_registry import response_schema_version
from advisory.wait_signals import WAIT_SIGNAL_MATCHES_TABLE, WAIT_SIGNALS_TABLE
from advisory.watchlist_builder import TABLE_NAME as WATCHLIST_TABLE
from utils.codex_cli import run_codex_structured
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.display_time import to_display_value
from utils.sync import parse_datetime_arg


env = Env()
env.read_env()
logger = logging.getLogger(__name__)

TABLE_NAME = "advisory_action_recommendations"
MANUAL_REVISION_PROMPT_ID = "manual_revision_pointers"
MANUAL_REVISION_PROMPT_VERSION = registry_prompt_version(MANUAL_REVISION_PROMPT_ID)
MANUAL_REVISION_PROMPT_SCHEMA_VERSION = response_schema_version(MANUAL_REVISION_PROMPT_ID)
CANDIDATES_TABLE = "advisory_candidates"
REGIME_TABLE = "advisory_market_regime"
EVENT_EVALUATIONS_TABLE = "advisory_event_evaluations"
EVENT_REVIEWS_TABLE = "advisory_event_reviews"
EVENT_POLICY_TABLE = "advisory_event_policy_actions"

ACTION_PRIORITY = {
    "SELL": 100,
    "PARTIAL_SELL": 90,
    "MANUAL_REVIEW": 80,
    "TIGHTEN_STOP": 70,
    "BUY_MORE": 60,
    "BUY": 50,
    "HOLD": 20,
    "WATCH": 10,
}
DEFAULT_CONFLICT_RULE_IDS = {
    "EXIT_BEATS_ENTRY_OR_WATCH",
    "ADVERSARIAL_VETO_MANUAL_BEATS_POSITIVE_OR_WATCH",
    "MARKET_GATE_MANUAL_BEATS_POSITIVE",
    "SAME_ACTION_DUPLICATE_COLLAPSE",
    "WATCH_LOSES_TO_HIGHER_PRIORITY",
}

PLAYBOOK_LOOKBACK_DAYS = 14
ACTION_MANUAL_REVISION_POINTERS_ENABLED = env.bool("ACTION_MANUAL_REVISION_POINTERS_ENABLED", default=False)
ACTION_MANUAL_REVISION_POINTERS_MODEL = env("ACTION_MANUAL_REVISION_POINTERS_MODEL", default="codex")
ACTION_MANUAL_REVISION_POINTERS_TIMEOUT_SECONDS = env.int("ACTION_MANUAL_REVISION_POINTERS_TIMEOUT_SECONDS", default=180)
ACTION_MANUAL_REVISION_POINTERS_MAX_CANDIDATES = env.int("ACTION_MANUAL_REVISION_POINTERS_MAX_CANDIDATES", default=8)

PLAYBOOK_ACTION_MAP = {
    "GO_CASH_REVIEW": ("MANUAL_REVIEW", None),
    "REDUCE_EXPOSURE_REVIEW": ("MANUAL_REVIEW", None),
    "SECTOR_REVIEW": ("MANUAL_REVIEW", None),
    "MANUAL_REVIEW": ("MANUAL_REVIEW", None),
    "BUY_WATCH": ("MANUAL_REVIEW", None),
    "WATCH_SYMBOLS": ("WATCH", None),
    "ADD_TO_WATCHLIST": ("WATCH", None),
    "NO_ACTION": ("HOLD", None),
}
EVENT_POLICY_ACTION_MAP = {
    "BUY_WATCH": ("MANUAL_REVIEW", None),
    "REDUCE_EXPOSURE_REVIEW": ("MANUAL_REVIEW", None),
    "MANUAL_REVIEW": ("MANUAL_REVIEW", None),
}
MANUAL_REVIEW_WAIT_SIGNAL_SETUP_ID = "MANUAL_REVIEW_WAIT_SIGNAL"

RISK_OFF_STATES = {"RISK_OFF", "HIGH", "STRESS", "CRASH", "HOSTILE"}
POSITIVE_BROKER_ACTIONS = {"BUY", "BUY_MORE"}
MARKET_CONTEXT_RISK_OFF_SCORE_THRESHOLD = env.float("ACTION_MARKET_CONTEXT_RISK_OFF_SCORE_THRESHOLD", default=0.60)
MARKET_CONTEXT_WEAK_BREADTH_THRESHOLD = env.float("ACTION_MARKET_CONTEXT_WEAK_BREADTH_THRESHOLD", default=40.0)
MARKET_CONTEXT_CAUTION_RISK_OFF_SCORE_THRESHOLD = env.float("ACTION_MARKET_CONTEXT_CAUTION_RISK_OFF_SCORE_THRESHOLD", default=0.45)
MARKET_CONTEXT_CAUTION_BREADTH_THRESHOLD = env.float("ACTION_MARKET_CONTEXT_CAUTION_BREADTH_THRESHOLD", default=50.0)
MARKET_CONTEXT_CAUTION_SIZE_MULTIPLIER = env.float("ACTION_MARKET_CONTEXT_CAUTION_SIZE_MULTIPLIER", default=0.75)


class ManualRevisionPointers(BaseModel):
    revision_summary: str = Field(min_length=10, description="One concise operator-facing summary of the final decision.")
    key_reasons: list[str] = Field(default_factory=list, description="Most important reasons supporting the chosen action.")
    manual_checks: list[str] = Field(default_factory=list, description="Specific checks the operator should do before acting.")
    risk_flags: list[str] = Field(default_factory=list, description="Risks, contradictions, stale evidence, or execution issues to verify.")
    missing_data: list[str] = Field(default_factory=list, description="Data that would improve confidence if available.")
    operator_questions: list[str] = Field(default_factory=list, description="Questions to answer during manual review.")


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


def table_columns(table_name: str) -> set[str]:
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
    return {str(value) for value in df["column_name"].dropna().tolist()}


def ensure_actions_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                asof_date TIMESTAMPTZ NOT NULL,
                published_on TIMESTAMPTZ,
                symbol TEXT NOT NULL,
                setup_id TEXT,
                unique_id TEXT,
                action_code TEXT NOT NULL,
                action_priority BIGINT,
                action_source TEXT,
                source_action TEXT,
                transaction_type TEXT,
                execution_mode TEXT,
                action_fraction DOUBLE PRECISION,
                approved_allocation_inr DOUBLE PRECISION,
                reference_price DOUBLE PRECISION,
                stop_price DOUBLE PRECISION,
                invalidation_price DOUBLE PRECISION,
                recommended_stop_price DOUBLE PRECISION,
                recommended_target_price DOUBLE PRECISION,
                expected_horizon_days BIGINT,
                invest_score_pct DOUBLE PRECISION,
                action_reason TEXT,
                action_detail TEXT,
                recommendation_reason_json TEXT,
                reason_contract_status TEXT,
                manual_revision_summary TEXT,
                manual_revision_pointers_json TEXT,
                manual_revision_prompt_id TEXT,
                manual_revision_prompt_version TEXT,
                manual_revision_prompt_schema_version TEXT,
                manual_revision_model TEXT,
                manual_revision_status TEXT,
                raw_context_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (asof_date, symbol)
            )
            """
        )
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS recommended_target_price DOUBLE PRECISION")
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS expected_horizon_days BIGINT")
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS recommendation_reason_json TEXT")
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS reason_contract_status TEXT")
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS manual_revision_summary TEXT")
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS manual_revision_pointers_json TEXT")
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS manual_revision_prompt_id TEXT")
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS manual_revision_prompt_version TEXT")
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS manual_revision_prompt_schema_version TEXT")
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS manual_revision_model TEXT")
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS manual_revision_status TEXT")


def _normalize_asof_date(asof_date: pd.Timestamp | None) -> pd.Timestamp:
    ts = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    return ts.normalize()


def _coerce_ts(value: Any) -> pd.Timestamp | None:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(ts) else ts


def _text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _num(value: Any) -> float | None:
    out = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(out) else float(out)


def _build_record(
    *,
    asof_date: pd.Timestamp,
    symbol: Any,
    setup_id: Any = None,
    unique_id: Any = None,
    published_on: Any = None,
    action_code: str,
    action_source: str,
    source_action: Any = None,
    transaction_type: Any = None,
    execution_mode: Any = None,
    action_fraction: Any = None,
    approved_allocation_inr: Any = None,
    reference_price: Any = None,
    stop_price: Any = None,
    invalidation_price: Any = None,
    recommended_stop_price: Any = None,
    recommended_target_price: Any = None,
    expected_horizon_days: Any = None,
    invest_score_pct: Any = None,
    action_reason: Any = None,
    action_detail: Any = None,
    raw_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_action = str(action_code).upper()
    return {
        "asof_date": asof_date,
        "published_on": _coerce_ts(published_on),
        "symbol": str(symbol).upper(),
        "setup_id": _text(setup_id),
        "unique_id": _text(unique_id),
        "action_code": normalized_action,
        "action_priority": int(ACTION_PRIORITY.get(normalized_action, 0)),
        "action_source": action_source,
        "source_action": _text(source_action),
        "transaction_type": _text(transaction_type),
        "execution_mode": _text(execution_mode),
        "action_fraction": _num(action_fraction),
        "approved_allocation_inr": _num(approved_allocation_inr),
        "reference_price": _num(reference_price),
        "stop_price": _num(stop_price),
        "invalidation_price": _num(invalidation_price),
        "recommended_stop_price": _num(recommended_stop_price),
        "recommended_target_price": _num(recommended_target_price),
        "expected_horizon_days": None if pd.isna(pd.to_numeric(expected_horizon_days, errors="coerce")) else int(pd.to_numeric(expected_horizon_days, errors="coerce")),
        "invest_score_pct": _num(invest_score_pct),
        "action_reason": _text(action_reason),
        "action_detail": _text(action_detail),
        "raw_context_json": json.dumps(raw_context or {}, ensure_ascii=False, default=str, sort_keys=True),
        "load_ts": pd.Timestamp.utcnow(),
    }


def load_portfolio_actions(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(PORTFOLIO_TABLE):
        return pd.DataFrame()
    clauses = ["asof_date = %s", "portfolio_status IN ('approved', 'trimmed')"]
    params: list[object] = [asof_date]
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([str(value).upper() for value in setup_ids])
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            published_on,
            setup_id,
            symbol,
            unique_id,
            portfolio_status,
            portfolio_reason,
            approved_allocation_inr,
            stop_price,
            invalidation_price,
            invest_score_pct,
            execution_notes
        FROM {PORTFOLIO_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC, symbol
        """,
        params=tuple(params),
    )


def load_lifecycle_actions(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(LIFECYCLE_TABLE):
        return pd.DataFrame()
    clauses = ["asof_date = %s"]
    params: list[object] = [asof_date]
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([str(value).upper() for value in setup_ids])
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            published_on,
            setup_id,
            symbol,
            unique_id,
            position_status,
            lifecycle_reason,
            next_action,
            next_action_reason,
            current_price,
            stop_price,
            invalidation_price
        FROM {LIFECYCLE_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC, symbol
        """,
        params=tuple(params),
    )


def load_rebalance_actions(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(REBALANCE_TABLE):
        return pd.DataFrame()
    clauses = ["asof_date = %s"]
    params: list[object] = [asof_date]
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([str(value).upper() for value in setup_ids])
    return sql_to_df(
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
            execution_mode
        FROM {REBALANCE_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC, symbol
        """,
        params=tuple(params),
    )


def load_watch_actions(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(WATCHLIST_TABLE):
        return pd.DataFrame()
    clauses = ["asof_date = %s", "COALESCE(current_state, watch_status, '') NOT ILIKE 'abstain%%'"]
    params: list[object] = [asof_date]
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([str(value).upper() for value in setup_ids])
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            state_updated_at AS published_on,
            setup_id,
            symbol,
            watch_status,
            current_state,
            candidate_state,
            watch_reason_detail,
            attractive_price_low,
            invalidation_price
        FROM {WATCHLIST_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY state_updated_at DESC, symbol
        """,
        params=tuple(params),
    )


def load_playbook_action_plans(*, asof_date: pd.Timestamp, symbols: list[str] | None = None) -> pd.DataFrame:
    ensure_hypothesis_tables()
    if not table_exists(ACTION_PLANS_TABLE):
        return pd.DataFrame()
    end_ts = asof_date + pd.Timedelta(days=1)
    start_ts = asof_date - pd.Timedelta(days=PLAYBOOK_LOOKBACK_DAYS)
    clauses = [
        "COALESCE(p.production_allowed, FALSE) = TRUE",
        "COALESCE(h.status, '') IN ('trusted_overlay', 'production')",
        "p.planned_at >= %s",
        "p.planned_at < %s",
    ]
    params: list[object] = [start_ts, end_ts]
    if symbols:
        clauses.append("(p.symbol IS NULL OR p.symbol = '' OR UPPER(p.symbol) = ANY(%s))")
        params.append([str(value).upper() for value in symbols])
    return sql_to_df(
        f"""
        SELECT p.*
        FROM {ACTION_PLANS_TABLE} p
        JOIN {HYPOTHESES_TABLE} h
          ON h.hypothesis_id = p.hypothesis_id
        WHERE {' AND '.join(clauses)}
        ORDER BY planned_at DESC, confidence DESC NULLS LAST
        """,
        params=tuple(params),
    )


def load_event_policy_actions(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(EVENT_POLICY_TABLE):
        return pd.DataFrame()
    clauses = [
        "asof_date = %s",
        "action_type IN ('BUY_WATCH', 'REDUCE_EXPOSURE_REVIEW', 'MANUAL_REVIEW')",
    ]
    params: list[object] = [asof_date]
    if symbols:
        clauses.append("UPPER(TRIM(symbol)) = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    if setup_ids:
        clauses.append("UPPER(TRIM(setup_id)) = ANY(%s)")
        params.append([str(value).upper() for value in setup_ids])
    return sql_to_df(
        f"""
        SELECT *
        FROM {EVENT_POLICY_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC NULLS LAST, policy_score DESC NULLS LAST
        """,
        params=tuple(params),
    )


def load_matched_manual_review_wait_signals(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(WAIT_SIGNALS_TABLE) or not table_exists(WAIT_SIGNAL_MATCHES_TABLE):
        return pd.DataFrame()
    if setup_ids:
        allowed_setup_ids = {str(value).strip().upper() for value in setup_ids if str(value or "").strip()}
        if MANUAL_REVIEW_WAIT_SIGNAL_SETUP_ID not in allowed_setup_ids and "MANUAL_REVIEW" not in allowed_setup_ids:
            return pd.DataFrame()
    end_ts = asof_date + pd.Timedelta(days=1)
    clauses = [
        "s.generated_by = 'manual_review_decision'",
        "COALESCE(m.match_status, 'matched') = 'matched'",
        "m.matched_at < %(end_ts)s",
    ]
    params: dict[str, Any] = {"end_ts": end_ts}
    if symbols:
        normalized_symbols = [str(value).strip().upper() for value in symbols if str(value or "").strip()]
        if normalized_symbols:
            clauses.append("UPPER(TRIM(COALESCE(s.symbol, m.symbol, ''))) = ANY(%(symbols)s)")
            params["symbols"] = normalized_symbols
    return sql_to_df(
        f"""
        SELECT DISTINCT ON (m.signal_id)
            m.matched_at,
            m.signal_id,
            m.hypothesis_id,
            m.symbol AS match_symbol,
            m.signal_type,
            m.expected_action,
            m.match_status,
            m.match_score,
            m.source_table AS match_source_table,
            m.source_key AS match_source_key,
            m.observed_at,
            m.observed_value,
            m.threshold_value,
            m.match_reason,
            m.evidence_json,
            s.created_at AS signal_created_at,
            s.hypothesis_title,
            s.source_table AS signal_source_table,
            s.source_key AS signal_source_key,
            s.symbol AS signal_symbol,
            s.status AS signal_status,
            s.priority,
            s.expected_action AS signal_expected_action,
            s.operator_summary,
            s.wait_question,
            s.condition_json,
            s.valid_until,
            s.generated_by
        FROM {WAIT_SIGNAL_MATCHES_TABLE} m
        JOIN {WAIT_SIGNALS_TABLE} s
          ON s.signal_id = m.signal_id
        WHERE {' AND '.join(clauses)}
        ORDER BY m.signal_id, m.matched_at DESC NULLS LAST
        """,
        params=params,
    )


def _parse_jsonish(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _json_context_value(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _merge_context(base: Any, *extras: dict[str, Any]) -> dict[str, Any]:
    context = _parse_jsonish(base, {})
    if not isinstance(context, dict):
        context = {}
    for extra in extras:
        for key, value in (extra or {}).items():
            normalized = _json_context_value(value)
            if normalized is None:
                continue
            existing = context.get(key)
            if key not in context or existing is None or existing == "":
                context[key] = normalized
    return context


def _load_latest_candidate_context(asof_date: pd.Timestamp, symbols: list[str]) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, dict[str, Any]]]:
    normalized_symbols = sorted({str(value).strip().upper() for value in symbols if str(value or "").strip()})
    if not normalized_symbols or not table_exists(CANDIDATES_TABLE):
        return {}, {}
    available = table_columns(CANDIDATES_TABLE)
    wanted = [
        "asof_date",
        "screener_date",
        "setup_id",
        "setup_name",
        "setup_family",
        "holding_horizon_note",
        "regime_name",
        "base_regime",
        "news_overlay",
        "theme_ids",
        "symbol",
        "screener_slug",
        "source_screener_slug",
        "source_screener_list",
        "rank",
        "candidate_state",
        "watch_reason_detail",
        "technical_state",
        "technical_trigger_type",
        "technical_trigger_note",
        "technical_trend_score",
        "technical_structure_score",
        "technical_participation_score",
        "technical_relative_strength_score",
        "technical_tradability_score",
        "technical_score",
        "fundamental_score",
        "regime_fit_score",
        "event_score",
        "setup_score",
        "avg_traded_value_20d",
        "rs_vs_benchmark",
        "rs_vs_sector",
        "intraday_breakout_score",
        "intraday_pattern_label",
        "entry_style",
        "attractive_price_low",
        "attractive_price_high",
        "invalidation_price",
        "entry_note",
        "near_miss_flag",
        "watch_reasons",
        "rule_pass",
        "load_ts",
    ]
    selected = [column for column in wanted if column in available]
    if not {"asof_date", "symbol", "setup_id"}.issubset(set(selected)):
        return {}, {}
    order_parts = ["asof_date DESC"]
    if "setup_score" in available:
        order_parts.append("setup_score DESC NULLS LAST")
    if "load_ts" in available:
        order_parts.append("load_ts DESC NULLS LAST")
    df = sql_to_df(
        f"""
        WITH ranked AS (
            SELECT
                {", ".join(selected)},
                ROW_NUMBER() OVER (
                    PARTITION BY UPPER(TRIM(symbol)), COALESCE(UPPER(TRIM(setup_id)), '')
                    ORDER BY {", ".join(order_parts)}
                ) AS rn
            FROM {CANDIDATES_TABLE}
            WHERE asof_date <= %(asof_date)s
              AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)
        )
        SELECT {", ".join(selected)}
        FROM ranked
        WHERE rn = 1
        """,
        params={"asof_date": asof_date, "symbols": normalized_symbols},
    )
    if df.empty:
        return {}, {}
    for column in ["asof_date", "screener_date", "load_ts"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["setup_id"] = df["setup_id"].astype("string").str.strip().str.upper()
    exact: dict[tuple[str, str], dict[str, Any]] = {}
    by_symbol: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        payload = {key: _json_context_value(value) for key, value in row.to_dict().items() if _json_context_value(value) is not None}
        symbol = str(payload.get("symbol") or "").upper()
        setup_id = str(payload.get("setup_id") or "").upper()
        if symbol and setup_id:
            exact[(setup_id, symbol)] = payload
        if symbol and symbol not in by_symbol:
            by_symbol[symbol] = payload
        elif symbol and "setup_score" in df.columns:
            current_score = pd.to_numeric(by_symbol[symbol].get("setup_score"), errors="coerce")
            next_score = pd.to_numeric(payload.get("setup_score"), errors="coerce")
            if pd.notna(next_score) and (pd.isna(current_score) or float(next_score) > float(current_score)):
                by_symbol[symbol] = payload
    return exact, by_symbol


def _load_regime_context(asof_date: pd.Timestamp) -> dict[str, Any]:
    if not table_exists(REGIME_TABLE):
        return {}
    available = table_columns(REGIME_TABLE)
    wanted = [
        "asof_date",
        "regime_name",
        "regime_notes",
        "shock_flag",
        "risk_off_flag",
        "macro_stress_score",
        "macro_risk_state",
        "macro_sizing_multiplier",
    ]
    selected = [column for column in wanted if column in available]
    if "asof_date" not in selected:
        return {}
    df = sql_to_df(
        f"""
        SELECT {", ".join(selected)}
        FROM {REGIME_TABLE}
        WHERE asof_date <= %s
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        params=(asof_date,),
    )
    if df.empty:
        return {}
    row = df.iloc[0].to_dict()
    return {
        ("regime_asof_date" if key == "asof_date" else key): _json_context_value(value)
        for key, value in row.items()
        if _json_context_value(value) is not None
    }


def _load_latest_event_context(asof_date: pd.Timestamp, symbols: list[str]) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, dict[str, Any]]]:
    normalized_symbols = sorted({str(value).strip().upper() for value in symbols if str(value or "").strip()})
    if not normalized_symbols or not table_exists(EVENT_EVALUATIONS_TABLE):
        return {}, {}
    available = table_columns(EVENT_EVALUATIONS_TABLE)
    wanted = [
        "published_on",
        "asof_date",
        "evaluated_at",
        "setup_id",
        "setup_name",
        "symbol",
        "unique_id",
        "event_source",
        "subject",
        "evaluation_status",
        "sentiment",
        "materiality",
        "setup_effect",
        "direction",
        "surprise",
        "novelty",
        "contradiction",
        "expected_decay_days",
        "source_reliability",
        "investable_now",
        "verdict",
        "event_class",
        "state_transition_hint",
        "score_impact",
        "confidence",
        "what_happened",
        "rationale",
        "event_tensor_json",
        "source_trace_json",
    ]
    selected = [column for column in wanted if column in available]
    if not {"published_on", "symbol", "setup_id", "unique_id"}.issubset(set(selected)):
        return {}, {}
    review_select = ""
    review_join = ""
    if table_exists(EVENT_REVIEWS_TABLE):
        review_available = table_columns(EVENT_REVIEWS_TABLE)
        review_cols = [
            "review_status",
            "review_action",
            "review_score",
            "veto",
            "review_reason",
            "review_flags_json",
        ]
        selected_review = [column for column in review_cols if column in review_available]
        if selected_review:
            review_select = ", " + ", ".join(f"r.{column} AS {column}" for column in selected_review)
            review_join = f"""
            LEFT JOIN {EVENT_REVIEWS_TABLE} r
              ON r.published_on = e.published_on
             AND r.setup_id = e.setup_id
             AND r.symbol = e.symbol
             AND r.unique_id = e.unique_id
            """
    df = sql_to_df(
        f"""
        WITH ranked AS (
            SELECT
                {", ".join(f"e.{column}" for column in selected)}
                {review_select},
                ROW_NUMBER() OVER (
                    PARTITION BY UPPER(TRIM(e.symbol)), COALESCE(UPPER(TRIM(e.setup_id)), '')
                    ORDER BY e.published_on DESC NULLS LAST, e.evaluated_at DESC NULLS LAST
                ) AS rn_setup,
                ROW_NUMBER() OVER (
                    PARTITION BY UPPER(TRIM(e.symbol))
                    ORDER BY e.published_on DESC NULLS LAST, e.evaluated_at DESC NULLS LAST
                ) AS rn_symbol
            FROM {EVENT_EVALUATIONS_TABLE} e
            {review_join}
            WHERE COALESCE(e.asof_date, e.published_on) <= %(asof_date)s
              AND UPPER(TRIM(e.symbol)) = ANY(%(symbols)s)
        )
        SELECT *
        FROM ranked
        WHERE rn_setup = 1 OR rn_symbol = 1
        """,
        params={"asof_date": asof_date + pd.Timedelta(days=1), "symbols": normalized_symbols},
    )
    if df.empty:
        return {}, {}
    for column in ["published_on", "asof_date", "evaluated_at"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["setup_id"] = df["setup_id"].astype("string").str.strip().str.upper()
    exact: dict[tuple[str, str], dict[str, Any]] = {}
    by_symbol: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        payload = {
            f"event_{key}" if key in {"asof_date", "published_on", "setup_id", "unique_id", "source_trace_json"} else key: _json_context_value(value)
            for key, value in row.to_dict().items()
            if key not in {"rn_setup", "rn_symbol"} and _json_context_value(value) is not None
        }
        symbol = str(row.get("symbol") or "").upper()
        setup_id = str(row.get("setup_id") or "").upper()
        if symbol and setup_id and int(row.get("rn_setup") or 0) == 1:
            exact[(setup_id, symbol)] = payload
        if symbol and int(row.get("rn_symbol") or 0) == 1:
            by_symbol[symbol] = payload
    return exact, by_symbol


def _load_latest_playbook_context(asof_date: pd.Timestamp, symbols: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    normalized_symbols = sorted({str(value).strip().upper() for value in symbols if str(value or "").strip()})
    if not table_exists(ACTION_PLANS_TABLE):
        return {}, {}
    available = table_columns(ACTION_PLANS_TABLE)
    wanted = [
        "planned_at",
        "hypothesis_id",
        "source_table",
        "source_key",
        "symbol",
        "trigger_scope",
        "suggested_action",
        "action_type",
        "urgency",
        "confidence",
        "production_allowed",
        "operator_summary",
        "decision_reason",
        "checks_json",
        "risk_controls_json",
        "action_plan_json",
        "llm_status",
    ]
    selected = [column for column in wanted if column in available]
    if not {"planned_at", "hypothesis_id", "source_key"}.issubset(set(selected)):
        return {}, {}
    clauses = ["planned_at <= %(asof_date)s"]
    params: dict[str, Any] = {"asof_date": asof_date + pd.Timedelta(days=1)}
    if "symbol" in selected and normalized_symbols:
        clauses.append("(symbol IS NULL OR symbol = '' OR UPPER(TRIM(symbol)) = ANY(%(symbols)s))")
        params["symbols"] = normalized_symbols
    df = sql_to_df(
        f"""
        WITH ranked AS (
            SELECT
                {", ".join(selected)},
                ROW_NUMBER() OVER (
                    PARTITION BY NULLIF(UPPER(TRIM(COALESCE(symbol, ''))), '')
                    ORDER BY planned_at DESC NULLS LAST, confidence DESC NULLS LAST
                ) AS rn_symbol,
                ROW_NUMBER() OVER (
                    PARTITION BY source_key
                    ORDER BY planned_at DESC NULLS LAST, confidence DESC NULLS LAST
                ) AS rn_source
            FROM {ACTION_PLANS_TABLE}
            WHERE {' AND '.join(clauses)}
        )
        SELECT *
        FROM ranked
        WHERE rn_symbol = 1 OR rn_source = 1
        """,
        params=params,
    )
    if df.empty:
        return {}, {}
    if "planned_at" in df.columns:
        df["planned_at"] = pd.to_datetime(df["planned_at"], utc=True, errors="coerce")
    by_symbol: dict[str, dict[str, Any]] = {}
    by_source: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        payload = {
            ("playbook_id" if key == "hypothesis_id" else f"playbook_{key}" if key in {"source_table", "source_key", "symbol", "planned_at"} else key): _json_context_value(value)
            for key, value in row.to_dict().items()
            if key not in {"rn_symbol", "rn_source"} and _json_context_value(value) is not None
        }
        symbol = str(row.get("symbol") or "").upper()
        source_key = str(row.get("source_key") or "")
        if symbol and int(row.get("rn_symbol") or 0) == 1:
            by_symbol[symbol] = payload
        if source_key and int(row.get("rn_source") or 0) == 1:
            by_source[source_key] = payload
    return by_symbol, by_source


def enrich_action_candidate_context(df: pd.DataFrame, *, asof_date: pd.Timestamp) -> pd.DataFrame:
    if df.empty or "symbol" not in df.columns:
        return df
    out = df.copy()
    symbols = out["symbol"].dropna().astype(str).str.upper().drop_duplicates().tolist()
    exact_candidates, symbol_candidates = _load_latest_candidate_context(asof_date, symbols)
    regime_context = _load_regime_context(asof_date)
    exact_events, symbol_events = _load_latest_event_context(asof_date, symbols)
    symbol_playbooks, source_playbooks = _load_latest_playbook_context(asof_date, symbols)
    enriched_contexts: list[str] = []
    for _, row in out.iterrows():
        symbol = str(row.get("symbol") or "").upper()
        setup_id = str(row.get("setup_id") or "").upper()
        unique_id = str(row.get("unique_id") or "")
        candidate_context = symbol_candidates.get(symbol, {})
        exact_context = exact_candidates.get((setup_id, symbol), {})
        event_context = symbol_events.get(symbol, {})
        exact_event_context = exact_events.get((setup_id, symbol), {})
        playbook_context = symbol_playbooks.get(symbol, {})
        source_playbook_context = source_playbooks.get(unique_id, {})
        context = _merge_context(
            row.get("raw_context_json"),
            regime_context,
            candidate_context,
            exact_context,
            event_context,
            exact_event_context,
            playbook_context,
            source_playbook_context,
            {
                "final_action_source": row.get("action_source"),
                "final_source_action": row.get("source_action"),
            },
        )
        parsed_sources = _parse_jsonish(context.get("context_sources"), [])
        context_sources = set(parsed_sources) if isinstance(parsed_sources, list) else set()
        if regime_context:
            context_sources.add("market_regime")
        if candidate_context or exact_context:
            context_sources.add("advisory_candidates")
        if event_context or exact_event_context:
            context_sources.add("advisory_event_evaluations")
        if playbook_context or source_playbook_context:
            context_sources.add("advisory_playbook_action_plans")
        if context_sources:
            context["context_sources"] = sorted(context_sources)
        enriched_contexts.append(json.dumps(context, ensure_ascii=False, default=str, sort_keys=True))
    out["raw_context_json"] = enriched_contexts
    return out


def _market_symbol_context(market_context: dict[str, Any], symbol: str) -> dict[str, Any]:
    rows = market_context.get("top_universe") if isinstance(market_context, dict) else []
    if not isinstance(rows, list):
        return {}
    normalized = str(symbol or "").strip().upper()
    for row in rows:
        if isinstance(row, dict) and str(row.get("symbol") or "").strip().upper() == normalized:
            return row
    return {}


def _market_context_adjustment(action_code: str, market_context: dict[str, Any], *, symbol: str | None = None) -> dict[str, Any]:
    summary = market_context.get("summary") if isinstance(market_context, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    action = str(action_code or "").strip().upper()
    regime_name = str(summary.get("regime_name") or "").strip().upper()
    macro_risk_state = str(summary.get("macro_risk_state") or "").strip().upper()
    breadth = pd.to_numeric(summary.get("breadth_trend_alignment_pct"), errors="coerce")
    risk_off_score = pd.to_numeric(summary.get("risk_off_score"), errors="coerce")
    macro_multiplier = pd.to_numeric(summary.get("macro_sizing_multiplier"), errors="coerce")
    symbol_context = _market_symbol_context(market_context, symbol or "")
    weak_breadth = bool(not pd.isna(breadth) and float(breadth) < MARKET_CONTEXT_WEAK_BREADTH_THRESHOLD)
    caution_breadth = bool(not pd.isna(breadth) and float(breadth) < MARKET_CONTEXT_CAUTION_BREADTH_THRESHOLD)
    risk_off = (
        regime_name in RISK_OFF_STATES
        or macro_risk_state in RISK_OFF_STATES
        or bool(not pd.isna(risk_off_score) and float(risk_off_score) >= MARKET_CONTEXT_RISK_OFF_SCORE_THRESHOLD)
    )
    caution = (
        not risk_off
        and (
            caution_breadth
            or bool(not pd.isna(risk_off_score) and float(risk_off_score) >= MARKET_CONTEXT_CAUTION_RISK_OFF_SCORE_THRESHOLD)
            or bool(not pd.isna(macro_multiplier) and float(macro_multiplier) < 1.0)
        )
    )
    adjusted_action = action
    adjustment = "none"
    reason = "Market context did not change the action boundary."
    size_multiplier = 1.0
    if action in POSITIVE_BROKER_ACTIONS and (risk_off or weak_breadth):
        adjusted_action = "MANUAL_REVIEW"
        adjustment = "positive_action_blocked_by_market_context"
        size_multiplier = 0.0
        reason = (
            "Positive broker action was blocked because broad market context is weak or risk-off "
            f"(regime={regime_name or 'n/a'}, macro_risk={macro_risk_state or 'n/a'}, "
            f"trend_breadth={None if pd.isna(breadth) else round(float(breadth), 2)}%, "
            f"risk_off_score={None if pd.isna(risk_off_score) else round(float(risk_off_score), 3)})."
        )
    elif action in POSITIVE_BROKER_ACTIONS and caution:
        adjustment = "positive_action_size_reduced_by_market_context"
        multiplier_candidates = [MARKET_CONTEXT_CAUTION_SIZE_MULTIPLIER]
        if not pd.isna(macro_multiplier):
            multiplier_candidates.append(float(macro_multiplier))
        size_multiplier = max(0.0, min(1.0, min(multiplier_candidates)))
        reason = (
            "Positive broker action size was reduced because broad market context is cautious "
            f"(regime={regime_name or 'n/a'}, macro_risk={macro_risk_state or 'n/a'}, "
            f"trend_breadth={None if pd.isna(breadth) else round(float(breadth), 2)}%, "
            f"risk_off_score={None if pd.isna(risk_off_score) else round(float(risk_off_score), 3)}, "
            f"size_multiplier={round(size_multiplier, 3)})."
        )
    return {
        "adjustment": adjustment,
        "original_action_code": action,
        "adjusted_action_code": adjusted_action,
        "reason": reason,
        "size_multiplier": size_multiplier,
        "regime_name": regime_name or None,
        "macro_risk_state": macro_risk_state or None,
        "breadth_trend_alignment_pct": None if pd.isna(breadth) else float(breadth),
        "risk_off_score": None if pd.isna(risk_off_score) else float(risk_off_score),
        "macro_sizing_multiplier": None if pd.isna(macro_multiplier) else float(macro_multiplier),
        "symbol_context": symbol_context,
    }


def apply_market_context_adjustments(df: pd.DataFrame, *, asof_date: pd.Timestamp) -> pd.DataFrame:
    if df.empty or "action_code" not in df.columns:
        return df
    market_context = load_latest_market_context(asof_date, limit=500)
    summary = market_context.get("summary") if isinstance(market_context, dict) else {}
    if not isinstance(summary, dict) or not summary:
        return df
    out = df.copy()
    for idx, row in out.iterrows():
        action = str(row.get("action_code") or "").strip().upper()
        if action not in POSITIVE_BROKER_ACTIONS:
            continue
        adjustment = _market_context_adjustment(action, market_context, symbol=str(row.get("symbol") or ""))
        if adjustment["adjustment"] == "none":
            continue
        raw_context = _merge_context(
            row.get("raw_context_json"),
            {
                "market_context_adjustment": adjustment["adjustment"],
                "market_context_adjustment_json": adjustment,
                "market_context_json": {
                    "summary": summary,
                    "symbol_context": adjustment.get("symbol_context") or {},
                },
            },
        )
        out.at[idx, "raw_context_json"] = json.dumps(raw_context, ensure_ascii=False, default=str, sort_keys=True)
        original_reason = _text(row.get("action_reason")) or "No original action reason supplied."
        out.at[idx, "action_detail"] = (
            f"{_text(row.get('action_detail')) or ''} Market context adjustment: {adjustment['reason']}"
        ).strip()
        if adjustment["adjustment"] == "positive_action_blocked_by_market_context":
            adjusted_action = str(adjustment["adjusted_action_code"])
            out.at[idx, "action_code"] = adjusted_action
            out.at[idx, "action_priority"] = int(ACTION_PRIORITY.get(adjusted_action, ACTION_PRIORITY["MANUAL_REVIEW"]))
            out.at[idx, "transaction_type"] = None
            out.at[idx, "execution_mode"] = "review_only"
            out.at[idx, "approved_allocation_inr"] = 0.0
            out.at[idx, "action_fraction"] = None
            out.at[idx, "action_reason"] = f"{adjustment['reason']} Original reason: {original_reason}"
        else:
            size_multiplier = float(adjustment.get("size_multiplier") or 1.0)
            for column in ["approved_allocation_inr", "action_fraction", "invest_score_pct"]:
                value = pd.to_numeric(out.at[idx, column], errors="coerce") if column in out.columns else pd.NA
                if pd.notna(value):
                    out.at[idx, column] = float(value) * size_multiplier
            out.at[idx, "action_reason"] = f"{original_reason} Market context reduced sizing: {adjustment['reason']}"
    return out


def load_enabled_conflict_rule_ids() -> set[str]:
    try:
        exists = table_exists(ACTION_CONFLICT_RULES_TABLE)
    except Exception as exc:
        logger.warning(
            "conflict rule table lookup failed; using default deterministic rules table=%s error=%s: %s",
            ACTION_CONFLICT_RULES_TABLE,
            type(exc).__name__,
            exc,
        )
        return set(DEFAULT_CONFLICT_RULE_IDS)
    if not exists:
        logger.warning(
            "conflict rule table missing; using default deterministic rules table=%s",
            ACTION_CONFLICT_RULES_TABLE,
        )
        return set(DEFAULT_CONFLICT_RULE_IDS)
    try:
        df = sql_to_df(
            f"""
            SELECT rule_id
            FROM {ACTION_CONFLICT_RULES_TABLE}
            WHERE COALESCE(enabled, TRUE)
            """,
            retries=2,
        )
    except Exception as exc:
        logger.warning(
            "conflict rule load failed; using default deterministic rules table=%s error=%s: %s",
            ACTION_CONFLICT_RULES_TABLE,
            type(exc).__name__,
            exc,
        )
        return set(DEFAULT_CONFLICT_RULE_IDS)
    return {str(value) for value in df["rule_id"].dropna().tolist()} if not df.empty else set()


def _norm_conflict_value(value: Any, *, upper: bool = True) -> str:
    text = "" if value is None else str(value).strip()
    return text.upper() if upper else text.lower()


def load_enabled_dynamic_conflict_rules_for_ranking() -> list[dict[str, Any]]:
    try:
        exists = table_exists(ACTION_CONFLICT_RULES_TABLE)
    except Exception as exc:
        logger.warning(
            "dynamic conflict rule table lookup failed; promoted conflict rules will not affect ranking table=%s error=%s: %s",
            ACTION_CONFLICT_RULES_TABLE,
            type(exc).__name__,
            exc,
        )
        return []
    if not exists:
        return []
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {ACTION_CONFLICT_RULES_TABLE}
            WHERE COALESCE(enabled, TRUE)
              AND condition_json IS NOT NULL
            ORDER BY priority DESC, rule_id
            """,
            retries=2,
        )
    except Exception as exc:
        logger.warning(
            "dynamic conflict rule load failed; promoted conflict rules will not affect ranking table=%s error=%s: %s",
            ACTION_CONFLICT_RULES_TABLE,
            type(exc).__name__,
            exc,
        )
        return []
    return df.to_dict(orient="records") if not df.empty else []


def _dynamic_conflict_rule_matches_candidate(row: pd.Series, peer: pd.Series, rule: dict[str, Any]) -> bool:
    condition = _parse_jsonish(rule.get("condition_json"), {})
    if not isinstance(condition, dict):
        return False
    if str(condition.get("condition_type") or "") != "action_pair_exact":
        return False
    if str(rule.get("resolution_action") or "keep_winner").strip() != "keep_winner":
        return False
    checks = [
        ("winning_action_code", row.get("action_code"), True),
        ("losing_action_code", peer.get("action_code"), True),
        ("winning_source", row.get("action_source"), False),
        ("losing_source", peer.get("action_source"), False),
    ]
    for field, actual, upper in checks:
        expected = condition.get(field)
        if expected in (None, ""):
            continue
        if _norm_conflict_value(actual, upper=upper) != _norm_conflict_value(expected, upper=upper):
            return False
    return True


def dynamic_conflict_precedence_for_row(row: pd.Series, peers: pd.DataFrame, dynamic_rules: list[dict[str, Any]]) -> dict[str, Any]:
    for rule in dynamic_rules:
        for _, peer in peers.iterrows():
            if peer.name == row.name:
                continue
            if not _dynamic_conflict_rule_matches_candidate(row, peer, rule):
                continue
            priority = pd.to_numeric(rule.get("priority"), errors="coerce")
            priority_score = 0 if pd.isna(priority) else int(priority)
            return {
                "score": 200 + priority_score,
                "rule_id": str(rule.get("rule_id") or ""),
                "reason": str(rule.get("resolution_reason") or "Matched operator-promoted exact conflict rule."),
            }
    return {"score": 0, "rule_id": None, "reason": None}


def conflict_precedence_for_row(
    row: pd.Series,
    enabled_rules: set[str],
    *,
    peers: pd.DataFrame | None = None,
    dynamic_rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    action = str(row.get("action_code") or "").strip().upper()
    raw_context = _parse_jsonish(row.get("raw_context_json"), {})
    if not isinstance(raw_context, dict):
        raw_context = {}
    context_text = json.dumps(raw_context, ensure_ascii=False, default=str).lower()
    dynamic_result = {"score": 0, "rule_id": None, "reason": None}
    if dynamic_rules and peers is not None and not peers.empty:
        dynamic_result = dynamic_conflict_precedence_for_row(row, peers, dynamic_rules)
    if "EXIT_BEATS_ENTRY_OR_WATCH" in enabled_rules and action in {"SELL", "PARTIAL_SELL", "TIGHTEN_STOP"}:
        return {
            "score": 400,
            "rule_id": "EXIT_BEATS_ENTRY_OR_WATCH",
            "reason": "Risk-management actions are selected before entry/watch/manual signals.",
        }
    if (
        "MARKET_GATE_MANUAL_BEATS_POSITIVE" in enabled_rules
        and action == "MANUAL_REVIEW"
        and ("market_context_adjustment" in context_text or "risk-off" in context_text or "risk_off" in context_text)
    ):
        return {
            "score": 300,
            "rule_id": "MARKET_GATE_MANUAL_BEATS_POSITIVE",
            "reason": "Risk-off market context keeps positive actions in manual review.",
        }
    if (
        "ADVERSARIAL_VETO_MANUAL_BEATS_POSITIVE_OR_WATCH" in enabled_rules
        and action == "MANUAL_REVIEW"
        and (
            raw_context.get("veto") is True
            or str(raw_context.get("review_action") or "").strip().lower() == "veto"
            or str(raw_context.get("action_status") or "").strip().lower() == "blocked_by_adversarial_review"
            or "adversarial review veto" in context_text
            or "blocked_by_adversarial_review" in context_text
        )
    ):
        return {
            "score": 350,
            "rule_id": "ADVERSARIAL_VETO_MANUAL_BEATS_POSITIVE_OR_WATCH",
            "reason": "Adversarial-review veto keeps the symbol in manual review ahead of buy/watch candidates.",
        }
    if int(dynamic_result.get("score") or 0) > 0:
        return dynamic_result
    if "WATCH_LOSES_TO_HIGHER_PRIORITY" in enabled_rules and action == "WATCH":
        return {
            "score": -100,
            "rule_id": "WATCH_LOSES_TO_HIGHER_PRIORITY",
            "reason": "Watch rows are informational unless no stronger action exists.",
        }
    return {"score": 0, "rule_id": None, "reason": None}


def apply_conflict_rule_precedence(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    enabled_rules = load_enabled_conflict_rule_ids()
    dynamic_rules = load_enabled_dynamic_conflict_rules_for_ranking()
    if not enabled_rules and not dynamic_rules:
        return df
    out = df.copy()
    out["_conflict_group_symbol"] = out["symbol"].astype("string").str.upper() if "symbol" in out.columns else ""
    out["_conflict_group_asof"] = pd.to_datetime(out["asof_date"], utc=True, errors="coerce") if "asof_date" in out.columns else pd.NaT
    scores: list[int] = []
    rule_ids: list[str | None] = []
    reasons: list[str | None] = []
    for _, row in out.iterrows():
        peers = out[
            (out["_conflict_group_symbol"] == row.get("_conflict_group_symbol"))
            & (out["_conflict_group_asof"] == row.get("_conflict_group_asof"))
        ]
        result = conflict_precedence_for_row(row, enabled_rules, peers=peers, dynamic_rules=dynamic_rules)
        scores.append(int(result.get("score") or 0))
        rule_ids.append(_text(result.get("rule_id")))
        reasons.append(_text(result.get("reason")))
    out["_conflict_rule_precedence"] = scores
    out["_conflict_precedence_rule_id"] = rule_ids
    out["_conflict_precedence_reason"] = reasons
    for idx, row in out.iterrows():
        rule_id = _text(row.get("_conflict_precedence_rule_id"))
        reason = _text(row.get("_conflict_precedence_reason"))
        if not rule_id:
            continue
        out.at[idx, "raw_context_json"] = json.dumps(
            _merge_context(
                row.get("raw_context_json"),
                {
                    "conflict_precedence_rule_id": rule_id,
                    "conflict_precedence_reason": reason,
                    "conflict_precedence_score": int(row.get("_conflict_rule_precedence") or 0),
                },
            ),
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )
    return out.drop(columns=["_conflict_group_symbol", "_conflict_group_asof"], errors="ignore")


def _json_ready_record(row: pd.Series | dict[str, Any]) -> dict[str, Any]:
    data = row.to_dict() if isinstance(row, pd.Series) else dict(row)
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str) and key.endswith("_json"):
            out[key] = _parse_jsonish(value, value)
            continue
        if isinstance(value, pd.Timestamp):
            out[key] = value.isoformat()
            continue
        try:
            if pd.isna(value):
                out[key] = None
                continue
        except Exception:
            pass
        out[key] = value
    return out


def _contract_section_from_context(context: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: context.get(key) for key in keys if context.get(key) is not None}


def _manual_review_contract_section(row: pd.Series, raw_context: dict[str, Any], action: str) -> dict[str, Any]:
    existing = _contract_section_from_context(
        raw_context,
        [
            "manual_review_boundary",
            "manual_review_effect",
            "operator_question",
            "review_reason",
            "blocked_original_action_code",
            "blocked_reason_contract_missing_fields",
            "broker_execution_allowed",
        ],
    )
    if action != "MANUAL_REVIEW":
        return existing

    action_source = _text(row.get("action_source"))
    source_action = _text(row.get("source_action"))
    execution_mode = _text(row.get("execution_mode"))
    transaction_type = _text(row.get("transaction_type"))
    review_only = execution_mode == "review_only" or not transaction_type
    inferred = {
        "manual_review_boundary": existing.get("manual_review_boundary") or "action_consolidation_manual_review",
        "manual_review_effect": existing.get("manual_review_effect") or "review_only_no_broker_execution",
        "operator_question": existing.get("operator_question")
        or raw_context.get("operator_question")
        or row.get("action_detail")
        or "Review the candidate evidence before any downstream portfolio, config, or execution change.",
        "review_reason": existing.get("review_reason") or row.get("action_reason"),
        "broker_execution_allowed": existing.get("broker_execution_allowed") if "broker_execution_allowed" in existing else (False if review_only else None),
        "manual_review_action_source": action_source or None,
        "manual_review_source_action": source_action or None,
    }
    rebalance_review_boundaries = {
        "review_manual": "lifecycle_rebalance_manual_review_required",
        "review_stale": "lifecycle_rebalance_stale_review_required",
        "review_horizon": "lifecycle_rebalance_horizon_review_required",
        "review_target": "lifecycle_rebalance_target_review_required",
    }
    if action_source == "rebalance" and source_action.lower() in rebalance_review_boundaries:
        inferred["manual_review_boundary"] = existing.get("manual_review_boundary") or rebalance_review_boundaries[source_action.lower()]
    elif action_source == "event_policy":
        inferred["manual_review_boundary"] = existing.get("manual_review_boundary") or "event_policy_review_required"
    elif action_source.startswith("playbook"):
        inferred["manual_review_boundary"] = existing.get("manual_review_boundary") or "playbook_review_required"
    market_adjustment = _parse_jsonish(raw_context.get("market_context_adjustment_json"), {})
    if not isinstance(market_adjustment, dict):
        market_adjustment = {}
    if raw_context.get("market_context_adjustment") == "positive_action_blocked_by_market_context":
        inferred["manual_review_boundary"] = (
            existing.get("manual_review_boundary") or "market_context_positive_action_review_required"
        )
        inferred["blocked_original_action_code"] = (
            existing.get("blocked_original_action_code") or market_adjustment.get("original_action_code") or None
        )
    if not existing.get("boundary") and inferred.get("manual_review_boundary"):
        inferred["boundary"] = inferred["manual_review_boundary"]
    if not existing.get("effect") and inferred.get("manual_review_effect"):
        inferred["effect"] = inferred["manual_review_effect"]
    if not existing.get("blocked_original_action"):
        inferred["blocked_original_action"] = inferred.get("blocked_original_action_code") or action
    return {key: value for key, value in {**existing, **inferred}.items() if value is not None}


def _same_action_candidate(row: pd.Series, item: dict[str, Any]) -> bool:
    for key in ["action_code", "action_source", "setup_id", "unique_id"]:
        left = _text(row.get(key))
        right = _text(item.get(key))
        if left != right:
            return False
    return True


def _same_symbol_conflict_section(row: pd.Series, candidates: pd.DataFrame | None) -> dict[str, Any]:
    if not isinstance(candidates, pd.DataFrame) or candidates.empty:
        return {}
    losing_candidates: list[dict[str, Any]] = []
    for item in candidates.to_dict(orient="records"):
        if _same_action_candidate(row, item):
            continue
        raw_context = _parse_jsonish(item.get("raw_context_json"), {})
        if not isinstance(raw_context, dict):
            raw_context = {}
        market_adjustment = _parse_jsonish(raw_context.get("market_context_adjustment_json"), {})
        if not isinstance(market_adjustment, dict):
            market_adjustment = {}
        losing_candidate = {
            "action_code": item.get("action_code"),
            "action_source": item.get("action_source"),
            "setup_id": item.get("setup_id"),
            "source_action": item.get("source_action"),
            "action_reason": item.get("action_reason"),
            "action_priority": item.get("action_priority"),
            "published_on": _json_context_value(item.get("published_on")),
        }
        if raw_context.get("market_context_adjustment"):
            losing_candidate.update(
                {
                    "market_context_adjustment": raw_context.get("market_context_adjustment"),
                    "original_action_code": market_adjustment.get("original_action_code"),
                    "market_context_adjustment_reason": market_adjustment.get("reason"),
                    "size_multiplier": market_adjustment.get("size_multiplier"),
                }
            )
        losing_candidates.append({key: value for key, value in losing_candidate.items() if value is not None})
    if not losing_candidates:
        return {}
    return {
        "same_symbol_candidate_count": int(len(candidates)),
        "same_symbol_conflict_count": int(len(losing_candidates)),
        "winning_action_code": row.get("action_code"),
        "winning_action_source": row.get("action_source"),
        "winning_action_priority": row.get("action_priority"),
        "source_precedence_reason": (
            f"Selected {row.get('action_code')} from {row.get('action_source')} over "
            f"{len(losing_candidates)} same-symbol candidate(s) by deterministic action priority, "
            "conflict-rule precedence, and freshness tie-breaks."
        ),
        "losing_candidates": losing_candidates[:5],
    }


def build_recommendation_reason_contract(row: pd.Series, candidates: pd.DataFrame | None = None) -> dict[str, Any]:
    raw_context = _parse_jsonish(row.get("raw_context_json"), {})
    if not isinstance(raw_context, dict):
        raw_context = {}
    action = str(row.get("action_code") or "").upper()
    execution_mode = _text(row.get("execution_mode"))
    action_reason = _text(row.get("action_reason"))
    action_detail = _text(row.get("action_detail"))
    risk_fields = {
        "reference_price": _num(row.get("reference_price")),
        "stop_price": _num(row.get("stop_price")),
        "invalidation_price": _num(row.get("invalidation_price")),
        "recommended_stop_price": _num(row.get("recommended_stop_price")),
        "recommended_target_price": _num(row.get("recommended_target_price")),
        "expected_horizon_days": None if pd.isna(pd.to_numeric(row.get("expected_horizon_days"), errors="coerce")) else int(pd.to_numeric(row.get("expected_horizon_days"), errors="coerce")),
        "action_fraction": _num(row.get("action_fraction")),
        "approved_allocation_inr": _num(row.get("approved_allocation_inr")),
    }
    risk_fields = {key: value for key, value in risk_fields.items() if value is not None}
    competing: list[dict[str, Any]] = []
    if isinstance(candidates, pd.DataFrame) and not candidates.empty:
        for item in candidates.head(max(1, ACTION_MANUAL_REVISION_POINTERS_MAX_CANDIDATES)).to_dict(orient="records"):
            competing.append(
                {
                    "action_code": item.get("action_code"),
                    "action_source": item.get("action_source"),
                    "setup_id": item.get("setup_id"),
                    "source_action": item.get("source_action"),
                    "action_reason": item.get("action_reason"),
                    "action_priority": item.get("action_priority"),
                }
            )
    conflict_resolution = _contract_section_from_context(
        raw_context,
        ["conflict_precedence_rule_id", "conflict_precedence_reason", "conflict_precedence_score"],
    )
    conflict_resolution = {**conflict_resolution, **_same_symbol_conflict_section(row, candidates)}
    evidence_sections = {
        "screener": _contract_section_from_context(raw_context, ["source_screener_slug", "source_screener_list", "screener_name"]),
        "technical": _contract_section_from_context(
            raw_context,
            [
                "technical_state",
                "technical_total_score",
                "technical_score",
                "technical_trigger_type",
                "pivot_price",
                "support_price",
                "setup_score",
            ],
        ),
        "event": _contract_section_from_context(
            raw_context,
            ["event_class", "verdict", "state_transition_hint", "score_impact", "review_action", "veto", "review_reason", "action_status"],
        ),
        "playbook": _contract_section_from_context(raw_context, ["playbook_id", "hypothesis_id", "action_type", "operator_summary", "decision_reason"]),
        "wait_signal": _contract_section_from_context(
            raw_context,
            ["wait_signal_followup", "signal_id", "match_reason", "condition_json", "wait_question"],
        ),
        "manual_review": _manual_review_contract_section(row, raw_context, action),
        "macro_regime": _contract_section_from_context(raw_context, ["macro_risk_state", "macro_stress_score", "regime_state", "market_regime"]),
        "conflict_resolution": conflict_resolution,
        "risk": risk_fields,
        "lifecycle": _contract_section_from_context(raw_context, ["position_status", "next_action", "suggested_action", "lifecycle_reason", "next_action_reason"]),
    }
    market_context = _parse_jsonish(raw_context.get("market_context_json"), {})
    if not isinstance(market_context, dict):
        market_context = {}
    market_summary = market_context.get("summary") if isinstance(market_context.get("summary"), dict) else {}
    market_symbol_context = market_context.get("symbol_context") if isinstance(market_context.get("symbol_context"), dict) else {}
    market_adjustment = _parse_jsonish(raw_context.get("market_context_adjustment_json"), {})
    if not isinstance(market_adjustment, dict):
        market_adjustment = {}
    market_context_section = {
        "market_context_adjustment": raw_context.get("market_context_adjustment"),
        "market_context_adjustment_reason": market_adjustment.get("reason"),
        "regime_name": market_adjustment.get("regime_name") or market_summary.get("regime_name"),
        "macro_risk_state": market_adjustment.get("macro_risk_state") or market_summary.get("macro_risk_state"),
        "breadth_trend_alignment_pct": market_adjustment.get("breadth_trend_alignment_pct") or market_summary.get("breadth_trend_alignment_pct"),
        "risk_off_score": market_adjustment.get("risk_off_score") or market_summary.get("risk_off_score"),
        "size_multiplier": market_adjustment.get("size_multiplier"),
        "macro_sizing_multiplier": market_adjustment.get("macro_sizing_multiplier") or market_summary.get("macro_sizing_multiplier"),
        "top_context_rank_pct": market_symbol_context.get("rank_pct"),
        "top_context_sector": market_symbol_context.get("sector_name") or market_symbol_context.get("sector_code"),
    }
    market_context_section = {key: value for key, value in market_context_section.items() if value is not None}
    if market_context_section:
        evidence_sections["macro_regime"] = {**evidence_sections.get("macro_regime", {}), **market_context_section}
    present_sections = [key for key, value in evidence_sections.items() if value]
    missing: list[str] = []
    if not action:
        missing.append("action_code")
    if not action_reason:
        missing.append("action_reason")
    if not row.get("action_source"):
        missing.append("action_source")
    if not present_sections:
        missing.append("evidence_context")
    if action in {"BUY", "BUY_MORE", "SELL", "PARTIAL_SELL"}:
        if not execution_mode:
            missing.append("execution_mode")
        if action in {"BUY", "BUY_MORE"} and not any(risk_fields.get(key) is not None for key in ["stop_price", "invalidation_price", "recommended_stop_price"]):
            missing.append("buy_risk_level")
        if action in {"SELL", "PARTIAL_SELL"} and not (action_detail or raw_context.get("suggested_action") or raw_context.get("next_action")):
            missing.append("sell_exit_trigger")
    if str(row.get("action_source") or "").startswith("playbook"):
        if not (raw_context.get("playbook_id") or raw_context.get("hypothesis_id")):
            missing.append("playbook_id")
        if not (row.get("unique_id") or raw_context.get("playbook_source_key") or raw_context.get("event_unique_id")):
            missing.append("playbook_source_key")
        if not (raw_context.get("checks_json") or raw_context.get("checks")):
            missing.append("playbook_review_checks")
    if action in {"BUY", "BUY_MORE"} and raw_context.get("event_class"):
        if not raw_context.get("event_unique_id"):
            missing.append("event_unique_id")
        if raw_context.get("review_action") is None:
            missing.append("event_review_action")
    status = "complete" if not missing else "incomplete"
    return {
        "schema_version": 1,
        "status": status,
        "missing_fields": missing,
        "symbol": str(row.get("symbol") or "").upper(),
        "action_code": action,
        "original_action_code": str(row.get("original_action_code") or market_adjustment.get("original_action_code") or action).upper(),
        "action_source": _text(row.get("action_source")),
        "source_action": _text(row.get("source_action")),
        "setup_id": _text(row.get("setup_id")),
        "unique_id": _text(row.get("unique_id")),
        "primary_reason": action_reason,
        "reason_detail": action_detail,
        "execution_mode": execution_mode,
        "transaction_type": _text(row.get("transaction_type")),
        "evidence_sections_present": present_sections,
        "evidence": evidence_sections,
        "competing_candidates": competing,
    }


def add_recommendation_reason_contracts(df: pd.DataFrame, all_candidates: pd.DataFrame | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    candidates = all_candidates if isinstance(all_candidates, pd.DataFrame) and not all_candidates.empty else out
    tmp = candidates.copy()
    tmp["asof_date"] = pd.to_datetime(tmp["asof_date"], utc=True, errors="coerce")
    candidate_groups: dict[tuple[Any, Any], pd.DataFrame] = {}
    for key, group in tmp.groupby(["asof_date", "symbol"], dropna=False):
        candidate_groups[key] = group.sort_values(["action_priority", "published_on"], ascending=[False, False], kind="stable")
    contracts: list[str] = []
    statuses: list[str] = []
    for idx, row in out.iterrows():
        asof_date = pd.to_datetime(row.get("asof_date"), utc=True, errors="coerce")
        group = candidate_groups.get((asof_date, row.get("symbol")), pd.DataFrame())
        contract = build_recommendation_reason_contract(row, group)
        if contract["status"] != "complete":
            original_action = str(row.get("action_code") or "").upper()
            original_context = _parse_jsonish(row.get("raw_context_json"), {})
            if not isinstance(original_context, dict):
                original_context = {}
            raw_missing = [str(value) for value in contract.get("missing_fields") or []]
            original_context.update(
                {
                    "manual_review_boundary": "incomplete_reason_contract",
                    "manual_review_effect": "review_only_no_broker_execution",
                    "operator_question": "Resolve the missing reason-contract fields before this recommendation can become broker-executable.",
                    "review_reason": "Recommendation was downgraded because required action evidence or risk controls were missing.",
                    "blocked_original_action_code": original_action,
                    "blocked_reason_contract_missing_fields": raw_missing,
                    "broker_execution_allowed": False,
                }
            )
            out.at[idx, "action_code"] = "MANUAL_REVIEW"
            out.at[idx, "action_priority"] = int(ACTION_PRIORITY["MANUAL_REVIEW"])
            out.at[idx, "transaction_type"] = None
            out.at[idx, "execution_mode"] = "review_only"
            out.at[idx, "raw_context_json"] = json.dumps(original_context, ensure_ascii=False, default=str, sort_keys=True)
            missing_text = ", ".join(contract.get("missing_fields") or [])
            existing_reason = _text(row.get("action_reason"))
            out.at[idx, "action_reason"] = f"Manual review required: incomplete reason contract ({missing_text})." if not existing_reason else f"Manual review required: incomplete reason contract ({missing_text}). Original reason: {existing_reason}"
            contract = build_recommendation_reason_contract(out.loc[idx], group)
            contract["status"] = "incomplete_downgraded"
            contract["original_action_code"] = original_action
            contract["missing_fields"] = contract.get("missing_fields") or []
            if missing_text and missing_text not in contract["missing_fields"]:
                contract["missing_fields"] = [*contract["missing_fields"], missing_text]
        contracts.append(json.dumps(contract, ensure_ascii=False, default=str, sort_keys=True))
        statuses.append(str(contract["status"]))
    out["recommendation_reason_json"] = contracts
    out["reason_contract_status"] = statuses
    return out


def _deterministic_manual_revision_pointers(row: pd.Series, candidates: pd.DataFrame, *, status: str = "deterministic") -> dict[str, Any]:
    action = str(row.get("action_code") or "ACTION").upper()
    symbol = str(row.get("symbol") or "").upper()
    source = str(row.get("action_source") or "unknown")
    reason = _text(row.get("action_reason")) or "No explicit reason was provided by the winning candidate."
    losing_actions = []
    if not candidates.empty:
        losing_actions = [
            f"{item.get('action_code')} from {item.get('action_source')}"
            for item in candidates.to_dict(orient="records")
            if str(item.get("action_code") or "").upper() != action or str(item.get("action_source") or "") != source
        ][:5]
    pointers = {
        "revision_summary": f"{symbol}: final action is {action} from {source}. {reason}",
        "key_reasons": [reason, f"Winning source: {source}"],
        "manual_checks": [
            "Confirm latest OHLCV/current price before acting.",
            "Check whether newer news, announcements, or exchange events contradict the decision.",
            "Verify stop/invalidation and position sizing before broker execution.",
        ],
        "risk_flags": losing_actions or ["No conflicting lower-priority candidate was available in the consolidation set."],
        "missing_data": [],
        "operator_questions": [
            "Is the evidence still fresh enough to act on?",
            "Does the current market/regime context still support this action?",
            "Would this action violate exposure, liquidity, or sector concentration limits?",
        ],
        "status": status,
    }
    return pointers


def _manual_revision_prompt(row: pd.Series, candidates: pd.DataFrame) -> str:
    payload = {
        "instruction": (
            "Create manual revision pointers for an investment operator. "
            "Do not change the final action. Do not recommend broker execution. "
            "Summarize what the operator should manually verify before accepting, rejecting, or modifying the decision."
        ),
        "final_decision": _json_ready_record(row),
        "competing_candidates": [
            _json_ready_record(item)
            for item in candidates.head(max(1, ACTION_MANUAL_REVISION_POINTERS_MAX_CANDIDATES)).to_dict(orient="records")
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def build_manual_revision_pointers(row: pd.Series, candidates: pd.DataFrame, *, use_llm: bool | None = None) -> tuple[dict[str, Any], str, str]:
    enabled = ACTION_MANUAL_REVISION_POINTERS_ENABLED if use_llm is None else bool(use_llm)
    model = ACTION_MANUAL_REVISION_POINTERS_MODEL
    if not enabled or str(model).strip().lower() in {"", "off", "none", "disabled", "false"}:
        return _deterministic_manual_revision_pointers(row, candidates, status="disabled"), model, "disabled"
    try:
        codex_model = model.split(":", 1)[1] if model.startswith("codex:") else None if model == "codex" else model
        result = run_codex_structured(
            _manual_revision_prompt(row, candidates),
            response_model=ManualRevisionPointers,
            model=codex_model,
            system_prompt=(
                "You are a cautious investment-operations reviewer. "
                "You produce concise manual review pointers for a human operator. "
                "You never override the decision, submit trades, or invent missing evidence."
            ),
            max_attempts=2,
            timeout_seconds=ACTION_MANUAL_REVISION_POINTERS_TIMEOUT_SECONDS,
        )
        pointers = result.model_dump()
        pointers["status"] = "ok"
        return pointers, model, "ok"
    except Exception as exc:
        pointers = _deterministic_manual_revision_pointers(row, candidates, status="fallback_after_error")
        pointers["llm_error"] = f"{type(exc).__name__}: {exc}"
        return pointers, model, "fallback_after_error"


def add_manual_revision_pointers(df: pd.DataFrame, all_candidates: pd.DataFrame | None = None, *, use_llm: bool | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    candidates = all_candidates if isinstance(all_candidates, pd.DataFrame) and not all_candidates.empty else out
    candidate_groups: dict[tuple[Any, Any], pd.DataFrame] = {}
    tmp = candidates.copy()
    tmp["asof_date"] = pd.to_datetime(tmp["asof_date"], utc=True, errors="coerce")
    for key, group in tmp.groupby(["asof_date", "symbol"], dropna=False):
        candidate_groups[key] = group.sort_values(["action_priority", "published_on"], ascending=[False, False], kind="stable")
    summaries: list[str | None] = []
    payloads: list[str | None] = []
    prompt_ids: list[str | None] = []
    prompt_versions: list[str | None] = []
    prompt_schema_versions: list[str | None] = []
    models: list[str | None] = []
    statuses: list[str | None] = []
    for _, row in out.iterrows():
        asof_date = pd.to_datetime(row.get("asof_date"), utc=True, errors="coerce")
        key = (asof_date, row.get("symbol"))
        group = candidate_groups.get(key, pd.DataFrame())
        pointers, model, status = build_manual_revision_pointers(row, group, use_llm=use_llm)
        summaries.append(_text(pointers.get("revision_summary")))
        payloads.append(json.dumps(pointers, ensure_ascii=False, default=str, sort_keys=True))
        prompt_ids.append(MANUAL_REVISION_PROMPT_ID)
        prompt_versions.append(MANUAL_REVISION_PROMPT_VERSION)
        prompt_schema_versions.append(MANUAL_REVISION_PROMPT_SCHEMA_VERSION)
        models.append(model)
        statuses.append(status)
    out["manual_revision_summary"] = summaries
    out["manual_revision_pointers_json"] = payloads
    out["manual_revision_prompt_id"] = prompt_ids
    out["manual_revision_prompt_version"] = prompt_versions
    out["manual_revision_prompt_schema_version"] = prompt_schema_versions
    out["manual_revision_model"] = models
    out["manual_revision_status"] = statuses
    return out


def _follow_up_window_days(row: pd.Series) -> int:
    plan = _parse_jsonish(row.get("action_plan_json"), {})
    out = pd.to_numeric(plan.get("follow_up_window_days") if isinstance(plan, dict) else None, errors="coerce")
    if pd.isna(out):
        out = 3
    return max(0, int(out))


def _playbook_plan_is_fresh(row: pd.Series, *, asof_date: pd.Timestamp) -> bool:
    planned_at = pd.to_datetime(row.get("planned_at"), utc=True, errors="coerce")
    if pd.isna(planned_at):
        return False
    expiry = planned_at.normalize() + pd.Timedelta(days=_follow_up_window_days(row) + 1)
    return asof_date < expiry


def _playbook_target_symbols(row: pd.Series, base_symbols: list[str], requested_symbols: list[str] | None) -> list[str]:
    symbol = _text(row.get("symbol"))
    if symbol:
        targets = [symbol.upper()]
    else:
        targets = [str(value).upper() for value in base_symbols if _text(value)]
    if requested_symbols:
        allowed = {str(value).upper() for value in requested_symbols}
        targets = [value for value in targets if value in allowed]
    return sorted(set(targets))


def build_playbook_action_candidates(
    *,
    asof_date: pd.Timestamp,
    base_symbols: list[str],
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    plans = load_playbook_action_plans(asof_date=asof_date, symbols=symbols)
    if plans.empty:
        return []
    if setup_ids:
        allowed_setup_ids = {str(value).upper() for value in setup_ids}
        plans = plans[plans["hypothesis_id"].astype("string").str.upper().isin(allowed_setup_ids)]
        if plans.empty:
            return []
    rows: list[dict[str, Any]] = []
    for _, row in plans.iterrows():
        if not _playbook_plan_is_fresh(row, asof_date=asof_date):
            continue
        action_type = str(row.get("action_type") or row.get("suggested_action") or "MANUAL_REVIEW").strip().upper()
        mapped_action, transaction_type = PLAYBOOK_ACTION_MAP.get(action_type, ("MANUAL_REVIEW", None))
        targets = _playbook_target_symbols(row, base_symbols, symbols)
        if not targets:
            continue
        risk_controls = _parse_jsonish(row.get("risk_controls_json"), {})
        checks = _parse_jsonish(row.get("checks_json"), [])
        for symbol in targets:
            rows.append(
                _build_record(
                    asof_date=asof_date,
                    published_on=row.get("planned_at"),
                    setup_id=row.get("hypothesis_id"),
                    symbol=symbol,
                    unique_id=row.get("source_key"),
                    action_code=mapped_action,
                    action_source="playbook_symbol" if _text(row.get("symbol")) else "playbook_market",
                    source_action=action_type,
                    transaction_type=transaction_type,
                    execution_mode="review_only",
                    action_fraction=risk_controls.get("max_fraction") if isinstance(risk_controls, dict) else None,
                    invest_score_pct=float(row.get("confidence") or 0.0) * 100.0,
                    expected_horizon_days=_follow_up_window_days(row),
                    action_reason=row.get("decision_reason"),
                    action_detail=row.get("operator_summary"),
                    raw_context={
                        **row.to_dict(),
                        "checks": checks,
                        "risk_controls": risk_controls,
                        "bridge_note": "Production playbook action plans are risk/review overlays only; they do not create broker-executable trades.",
                    },
                )
            )
    return rows


def build_event_policy_action_candidates(
    *,
    asof_date: pd.Timestamp,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    policies = load_event_policy_actions(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
    if policies.empty:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in policies.iterrows():
        action_type = str(row.get("action_type") or "MANUAL_REVIEW").strip().upper()
        mapped_action, transaction_type = EVENT_POLICY_ACTION_MAP.get(action_type, ("MANUAL_REVIEW", None))
        confidence = _num(row.get("confidence")) or 0.0
        invest_score_pct = max(0.0, min(100.0, confidence * 100.0 if confidence <= 1.0 else confidence))
        rows.append(
            _build_record(
                asof_date=asof_date,
                published_on=row.get("published_on"),
                setup_id=row.get("setup_id"),
                symbol=row.get("symbol"),
                unique_id=row.get("unique_id"),
                action_code=mapped_action,
                action_source="event_policy",
                source_action=action_type,
                transaction_type=transaction_type,
                execution_mode="review_only",
                invest_score_pct=invest_score_pct,
                expected_horizon_days=None,
                action_reason=row.get("action_reason"),
                action_detail=row.get("action_detail"),
                raw_context={
                    **row.to_dict(),
                    "event_policy_bridge_note": "Deterministic event policies are review/risk overlays only; they do not create broker-executable trades.",
                    "policy_checks": _parse_jsonish(row.get("checks_json"), []),
                    "policy_raw_context": _parse_jsonish(row.get("raw_context_json"), {}),
                },
            )
        )
    return rows


def _matched_wait_signal_context(row: pd.Series) -> dict[str, Any]:
    evidence = _parse_jsonish(row.get("evidence_json"), {})
    if not isinstance(evidence, dict):
        evidence = {}
    wait_signal_context = evidence.get("wait_signal") if isinstance(evidence.get("wait_signal"), dict) else {}
    condition = _parse_jsonish(row.get("condition_json"), {})
    if not isinstance(condition, dict):
        condition = {}
    return {
        "signal_id": row.get("signal_id"),
        "match_source_table": row.get("match_source_table"),
        "match_source_key": row.get("match_source_key"),
        "match_reason": row.get("match_reason"),
        "manual_review_item_id": wait_signal_context.get("manual_review_item_id") or row.get("signal_source_key"),
        "manual_review_source_table": wait_signal_context.get("manual_review_source_table") or row.get("signal_source_table"),
        "manual_review_source_key": wait_signal_context.get("manual_review_source_key") or row.get("signal_source_key"),
        "wait_question": wait_signal_context.get("wait_question") or row.get("wait_question"),
        "condition_type": condition.get("condition_type") or row.get("signal_type"),
        "condition": condition,
        "evidence": evidence,
        "bridge_note": "Matched Manual Review wait signals become review-only action candidates; they do not create broker-executable trades.",
    }


def build_matched_wait_signal_action_candidates(
    *,
    asof_date: pd.Timestamp,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    matches = load_matched_manual_review_wait_signals(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
    if matches.empty:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in matches.iterrows():
        symbol = _text(row.get("signal_symbol")) or _text(row.get("match_symbol"))
        if not symbol:
            continue
        context = _matched_wait_signal_context(row)
        confidence = _num(row.get("match_score"))
        invest_score_pct = None if confidence is None else max(0.0, min(100.0, confidence * 100.0 if confidence <= 1.0 else confidence))
        wait_question = _text(context.get("wait_question"))
        reason_bits = [
            "Manual Review wait signal matched fresh evidence.",
            _text(row.get("match_reason")),
        ]
        if wait_question:
            reason_bits.append(f"Original wait: {wait_question}")
        rows.append(
            _build_record(
                asof_date=asof_date,
                published_on=row.get("matched_at") or row.get("observed_at"),
                setup_id=MANUAL_REVIEW_WAIT_SIGNAL_SETUP_ID,
                symbol=symbol,
                unique_id=f"{row.get('signal_id')}:{row.get('match_source_table')}:{row.get('match_source_key')}",
                action_code="MANUAL_REVIEW",
                action_source="manual_review_wait_signal",
                source_action=row.get("signal_expected_action") or row.get("expected_action") or "MANUAL_REVIEW",
                transaction_type=None,
                execution_mode="review_only",
                invest_score_pct=invest_score_pct,
                action_reason=" ".join(part for part in reason_bits if part),
                action_detail=row.get("operator_summary"),
                raw_context={**row.to_dict(), "wait_signal_followup": context},
            )
        )
    return rows


def build_action_recommendations(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> pd.DataFrame:
    monitor_date = _normalize_asof_date(asof_date)
    candidates: list[dict[str, Any]] = []

    portfolio_df = load_portfolio_actions(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
    for _, row in portfolio_df.iterrows():
        candidates.append(
            _build_record(
                asof_date=monitor_date,
                published_on=row.get("published_on"),
                setup_id=row.get("setup_id"),
                symbol=row.get("symbol"),
                unique_id=row.get("unique_id"),
                action_code="BUY",
                action_source="portfolio",
                source_action=row.get("portfolio_status"),
                transaction_type="BUY",
                execution_mode="broker_order",
                approved_allocation_inr=row.get("approved_allocation_inr"),
                stop_price=row.get("stop_price"),
                invalidation_price=row.get("invalidation_price"),
                invest_score_pct=row.get("invest_score_pct"),
                action_reason=row.get("portfolio_reason"),
                action_detail=row.get("execution_notes"),
                raw_context=row.to_dict(),
            )
        )

    lifecycle_df = load_lifecycle_actions(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
    for _, row in lifecycle_df.iterrows():
        if str(row.get("position_status") or "").lower() != "open":
            continue
        candidates.append(
            _build_record(
                asof_date=monitor_date,
                published_on=row.get("published_on"),
                setup_id=row.get("setup_id"),
                symbol=row.get("symbol"),
                unique_id=row.get("unique_id"),
                action_code="HOLD",
                action_source="lifecycle",
                source_action=row.get("next_action"),
                transaction_type=None,
                execution_mode="review_only",
                reference_price=row.get("current_price"),
                stop_price=row.get("stop_price"),
                invalidation_price=row.get("invalidation_price"),
                action_reason=row.get("lifecycle_reason"),
                action_detail=row.get("next_action_reason"),
                raw_context=row.to_dict(),
            )
        )

    rebalance_df = load_rebalance_actions(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
    action_map = {
        "exit_invalidation": ("SELL", "SELL"),
        "exit_stop": ("SELL", "SELL"),
        "exit_emergency": ("SELL", "SELL"),
        "exit_technical_failure": ("SELL", "SELL"),
        "exit_time_stop": ("SELL", "SELL"),
        "trim_winner": ("PARTIAL_SELL", "SELL"),
        "add_on_pullback": ("BUY_MORE", "BUY"),
        "tighten_stop": ("TIGHTEN_STOP", None),
        "review_manual": ("MANUAL_REVIEW", None),
        "review_stale": ("MANUAL_REVIEW", None),
        "review_horizon": ("MANUAL_REVIEW", None),
        "review_target": ("MANUAL_REVIEW", None),
    }
    for _, row in rebalance_df.iterrows():
        source_action = str(row.get("suggested_action") or "").strip().lower()
        mapped = action_map.get(source_action, ("MANUAL_REVIEW", None))
        candidates.append(
            _build_record(
                asof_date=monitor_date,
                published_on=row.get("published_on"),
                setup_id=row.get("setup_id"),
                symbol=row.get("symbol"),
                unique_id=row.get("unique_id"),
                action_code=mapped[0],
                action_source="rebalance",
                source_action=row.get("suggested_action"),
                transaction_type=mapped[1],
                execution_mode=row.get("execution_mode") or ("broker_order" if mapped[1] else "review_only"),
                action_fraction=row.get("action_fraction"),
                reference_price=row.get("reference_price"),
                stop_price=row.get("stop_price"),
                invalidation_price=row.get("invalidation_price"),
                recommended_stop_price=row.get("recommended_stop_price"),
                recommended_target_price=row.get("recommended_target_price"),
                expected_horizon_days=row.get("expected_horizon_days"),
                action_reason=row.get("action_reason"),
                action_detail=row.get("suggested_action"),
                raw_context=row.to_dict(),
            )
        )

    watch_df = load_watch_actions(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
    for _, row in watch_df.iterrows():
        candidates.append(
            _build_record(
                asof_date=monitor_date,
                published_on=row.get("published_on"),
                setup_id=row.get("setup_id"),
                symbol=row.get("symbol"),
                unique_id=None,
                action_code="WATCH",
                action_source="watchlist",
                source_action=row.get("current_state") or row.get("watch_status"),
                transaction_type=None,
                execution_mode="review_only",
                reference_price=row.get("attractive_price_low"),
                invalidation_price=row.get("invalidation_price"),
                action_reason=row.get("watch_reason_detail"),
                action_detail=row.get("candidate_state"),
                raw_context=row.to_dict(),
            )
        )

    candidates.extend(
        build_event_policy_action_candidates(
            asof_date=monitor_date,
            symbols=symbols,
            setup_ids=setup_ids,
        )
    )
    candidates.extend(
        build_matched_wait_signal_action_candidates(
            asof_date=monitor_date,
            symbols=symbols,
            setup_ids=setup_ids,
        )
    )

    base_symbols = sorted(
        {
            str(row.get("symbol")).upper()
            for row in candidates
            if _text(row.get("symbol"))
        }
    )
    candidates.extend(
        build_playbook_action_candidates(
            asof_date=monitor_date,
            base_symbols=base_symbols,
            symbols=symbols,
            setup_ids=setup_ids,
        )
    )

    if not candidates:
        return pd.DataFrame()

    candidate_df = enrich_action_candidate_context(pd.DataFrame(candidates), asof_date=monitor_date)
    candidate_df = apply_market_context_adjustments(candidate_df, asof_date=monitor_date)
    winners = rank_action_candidates(candidate_df)
    winners = add_recommendation_reason_contracts(winners, candidate_df)
    winners = add_manual_revision_pointers(winners, candidate_df)
    winners.attrs["all_action_candidates"] = candidate_df
    return winners


def rank_action_candidates(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    ranked = apply_conflict_rule_precedence(df)
    ranked["symbol"] = ranked["symbol"].astype("string").str.upper()
    ranked["published_on"] = pd.to_datetime(ranked["published_on"], utc=True, errors="coerce")
    ranked["action_priority"] = pd.to_numeric(ranked["action_priority"], errors="coerce").fillna(0).astype(int)
    if "_conflict_rule_precedence" not in ranked.columns:
        ranked["_conflict_rule_precedence"] = 0
    ranked["_conflict_rule_precedence"] = pd.to_numeric(ranked["_conflict_rule_precedence"], errors="coerce").fillna(0).astype(int)
    if "invest_score_pct" not in ranked.columns:
        ranked["invest_score_pct"] = pd.NA
    ranked["invest_score_pct"] = pd.to_numeric(ranked["invest_score_pct"], errors="coerce")
    ranked = ranked.sort_values(
        ["symbol", "_conflict_rule_precedence", "action_priority", "published_on", "invest_score_pct", "setup_id"],
        ascending=[True, False, False, False, False, True],
        kind="stable",
    )
    winners = ranked.drop_duplicates(subset=["asof_date", "symbol"], keep="first").reset_index(drop=True)
    return winners.drop(columns=[column for column in ["_conflict_rule_precedence", "_conflict_precedence_rule_id", "_conflict_precedence_reason"] if column in winners.columns])


def persist_action_recommendations(df: pd.DataFrame) -> None:
    ensure_actions_table()
    if df.empty:
        return
    all_candidates = df.attrs.get("all_action_candidates")
    if not isinstance(all_candidates, pd.DataFrame) or all_candidates.empty:
        all_candidates = df
    asof_series = all_candidates["asof_date"] if "asof_date" in all_candidates.columns else pd.Series([], dtype=object)
    asof_values = pd.to_datetime(asof_series, utc=True, errors="coerce").dropna()
    enrich_asof = asof_values.max().normalize() if not asof_values.empty else _normalize_asof_date(None)
    all_candidates = enrich_action_candidate_context(all_candidates, asof_date=enrich_asof)
    all_candidates = apply_market_context_adjustments(all_candidates, asof_date=enrich_asof)
    all_candidates = apply_conflict_rule_precedence(all_candidates)
    winners = rank_action_candidates(all_candidates)
    existing_reason_columns = {"recommendation_reason_json", "reason_contract_status"}
    if not existing_reason_columns.issubset(set(winners.columns)) or winners["recommendation_reason_json"].isna().any():
        winners = add_recommendation_reason_contracts(winners, all_candidates)
    existing_pointer_columns = {
        "manual_revision_summary",
        "manual_revision_pointers_json",
        "manual_revision_prompt_id",
        "manual_revision_prompt_version",
        "manual_revision_prompt_schema_version",
        "manual_revision_model",
        "manual_revision_status",
    }
    if not existing_pointer_columns.issubset(set(winners.columns)) or winners["manual_revision_pointers_json"].isna().any():
        winners = add_manual_revision_pointers(winners, all_candidates)
    conflicts = build_action_conflicts(all_candidates, winners)
    out = winners.copy()
    out = out.drop(columns=[column for column in ["_conflict_rule_precedence", "_conflict_precedence_rule_id", "_conflict_precedence_reason"] if column in out.columns])
    for column in [
        "action_fraction",
        "approved_allocation_inr",
        "reference_price",
        "stop_price",
        "invalidation_price",
        "recommended_stop_price",
        "recommended_target_price",
        "invest_score_pct",
    ]:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    if "expected_horizon_days" in out.columns:
        out["expected_horizon_days"] = pd.to_numeric(out["expected_horizon_days"], errors="coerce").astype("Int64")
    for column in ["asof_date", "published_on", "load_ts"]:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    with db_session() as (_, cur):
        pairs = (
            out[["asof_date", "symbol"]]
            .dropna()
            .drop_duplicates()
            .to_dict(orient="records")
        )
        for item in pairs:
            cur.execute(
                f"DELETE FROM {TABLE_NAME} WHERE asof_date = %s AND symbol = %s",
                (
                    pd.to_datetime(item["asof_date"], utc=True, errors="coerce").to_pydatetime(),
                    str(item["symbol"]).upper(),
                ),
            )
    upsert_to_db(
        out,
        TABLE_NAME,
        unique_keys=["asof_date", "symbol"],
        timescaledb_column="asof_date",
    )
    persist_action_conflicts(conflicts)
    _trace_action_consolidation(all_candidates, winners, conflicts)


def _trace_action_consolidation(all_candidates: pd.DataFrame, winners: pd.DataFrame, conflicts: pd.DataFrame) -> None:
    if winners.empty:
        return
    candidate_groups: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    if not all_candidates.empty:
        tmp = all_candidates.copy()
        tmp["asof_date"] = pd.to_datetime(tmp["asof_date"], utc=True, errors="coerce")
        for key, group in tmp.groupby(["asof_date", "symbol"], dropna=False):
            candidate_groups[key] = group.sort_values("action_priority", ascending=False).head(10).to_dict(orient="records")
    conflict_counts: dict[tuple[Any, Any], int] = {}
    if not conflicts.empty:
        tmp_conflicts = conflicts.copy()
        tmp_conflicts["asof_date"] = pd.to_datetime(tmp_conflicts["asof_date"], utc=True, errors="coerce")
        conflict_counts = tmp_conflicts.groupby(["asof_date", "symbol"], dropna=False).size().to_dict()
    for _, row in winners.iterrows():
        asof_date = pd.to_datetime(row.get("asof_date"), utc=True, errors="coerce")
        key = (asof_date, row.get("symbol"))
        candidate_rows = candidate_groups.get(key, [])
        trace_id = safe_trace_call(
            append_trace,
            asof_date=row.get("asof_date"),
            symbol=row.get("symbol"),
            unique_id=row.get("unique_id"),
            setup_id=row.get("setup_id"),
            trigger_type="action_consolidation",
            final_action=row.get("action_code"),
            final_reason=row.get("action_reason"),
            source_table=TABLE_NAME,
            source_key=f"{row.get('asof_date')}:{row.get('symbol')}",
            payload={
                "action_source": row.get("action_source"),
                "action_priority": row.get("action_priority"),
                "conflict_count": int(conflict_counts.get(key, 0)),
                "candidate_count": len(candidate_rows),
                "reason_contract_status": row.get("reason_contract_status"),
                "recommendation_reason": _parse_jsonish(row.get("recommendation_reason_json"), {}),
                "manual_revision_summary": row.get("manual_revision_summary"),
                "manual_revision_pointers": _parse_jsonish(row.get("manual_revision_pointers_json"), {}),
                "manual_revision_status": row.get("manual_revision_status"),
            },
        )
        if trace_id:
            if str(row.get("action_source") or "").startswith("playbook"):
                safe_trace_call(
                    append_trace_step,
                    trace_id=trace_id,
                    step_idx=45,
                    stage="playbook_action_plan",
                    status="review_overlay",
                    reason=row.get("action_reason"),
                    input_payload=row.get("raw_context_json"),
                    output_payload=row.to_dict(),
                    payload={
                        "playbook_id": row.get("setup_id"),
                        "source_key": row.get("unique_id"),
                        "source_action": row.get("source_action"),
                        "mapped_action": row.get("action_code"),
                        "production_allowed": True,
                        "execution_mode": row.get("execution_mode"),
                        "confidence": row.get("invest_score_pct"),
                        "operator_summary": row.get("action_detail"),
                        "review_boundary": "Playbook candidates are review/risk overlays only, not direct broker orders.",
                        "reason_contract_status": row.get("reason_contract_status"),
                        "recommendation_reason": _parse_jsonish(row.get("recommendation_reason_json"), {}),
                        "manual_revision_summary": row.get("manual_revision_summary"),
                        "manual_revision_pointers": _parse_jsonish(row.get("manual_revision_pointers_json"), {}),
                    },
                )
            safe_trace_call(
                append_trace_step,
                trace_id=trace_id,
                step_idx=50,
                stage="action_consolidation",
                status="completed",
                reason=row.get("action_reason"),
                input_payload=candidate_rows,
                output_payload=row.to_dict(),
                payload={
                    "winner_action": row.get("action_code"),
                    "winner_source": row.get("action_source"),
                    "candidate_count": len(candidate_rows),
                    "conflict_count": int(conflict_counts.get(key, 0)),
                    "reason_contract_status": row.get("reason_contract_status"),
                    "recommendation_reason": _parse_jsonish(row.get("recommendation_reason_json"), {}),
                    "manual_revision_summary": row.get("manual_revision_summary"),
                    "manual_revision_pointers": _parse_jsonish(row.get("manual_revision_pointers_json"), {}),
                    "manual_revision_model": row.get("manual_revision_model"),
                    "manual_revision_status": row.get("manual_revision_status"),
                },
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build one consolidated action recommendation per symbol for operator and execution flows.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--setup", dest="setup_ids", nargs="*")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    df = build_action_recommendations(asof_date=asof_date, symbols=args.symbols, setup_ids=args.setup_ids)
    if not args.dry_run:
        persist_action_recommendations(df)
    result = {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(df)),
        "action_counts": df["action_code"].value_counts(dropna=False).to_dict() if not df.empty else {},
        "sample": to_display_value(df.head(10)),
        "dry_run": bool(args.dry_run),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
