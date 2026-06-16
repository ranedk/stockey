from __future__ import annotations

import argparse
import json
from typing import Any, Literal

import pandas as pd
from environs import Env
from pydantic import BaseModel, Field

from advisory.fallback_telemetry import record_fallback_event, record_local_fallback_event
from advisory.prompt_registry import prompt_version as registry_prompt_version
from advisory.prompt_registry import response_schema_version
from utils.codex_cli import run_codex_structured
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


env = Env()
env.read_env()

TABLE_NAME = "advisory_company_memory_reviews"
COMPANY_MEMORY_SCHEMA_MIGRATION_ID = "20260611_advisory_company_memory_reviews_base"
ANNOUNCEMENT_EVIDENCE_TABLE = "advisory_announcement_evidence"
BHAVCOPY_EVIDENCE_TABLE = "advisory_bhavcopy_evidence_daily"
TECHNICAL_TABLE = "advisory_technical_daily"
CANDIDATES_TABLE = "advisory_candidates"
WATCHLIST_TABLE = "advisory_watchlist"
EVENT_EVALUATIONS_TABLE = "advisory_event_evaluations"
EVENT_POLICY_TABLE = "advisory_event_policy_actions"
ACTIONS_TABLE = "advisory_action_recommendations"
WAIT_SIGNALS_TABLE = "advisory_wait_signals"
PROMPT_ID = "company_memory_review"
PROMPT_VERSION = registry_prompt_version(PROMPT_ID)
PROMPT_SCHEMA_VERSION = response_schema_version(PROMPT_ID)

DEFAULT_MODEL = env("COMPANY_MEMORY_REVIEW_MODEL", default=env("CODEX_CLI_MODEL", default="gpt-5.4-mini"))
DEFAULT_MAX_SYMBOLS = env.int("COMPANY_MEMORY_REVIEW_MAX_SYMBOLS", default=12)
DEFAULT_LOOKBACK_DAYS = env.int("COMPANY_MEMORY_REVIEW_LOOKBACK_DAYS", default=180)
LLM_ENABLED = env.bool("COMPANY_MEMORY_REVIEW_LLM_ENABLED", default=False)

SIGNALS = {"BUY", "BUY_MORE", "HOLD", "WATCH", "SELL_PARTIAL", "SELL", "NO_ACTION"}
COMPANY_MEMORY_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        review_date TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        recommended_signal TEXT NOT NULL,
        confidence DOUBLE PRECISION,
        conviction_score DOUBLE PRECISION,
        summary TEXT,
        thesis TEXT,
        risk_flags_json TEXT,
        evidence_used_json TEXT,
        wait_for_json TEXT,
        deterministic_boundary TEXT,
        authority_scope TEXT,
        model_name TEXT,
        prompt_id TEXT,
        prompt_version TEXT,
        prompt_schema_version TEXT,
        review_status TEXT,
        fallback_used BOOLEAN,
        error TEXT,
        payload_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (review_date, symbol)
    )
    """,
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS risk_flags_json TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS evidence_used_json TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS wait_for_json TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS deterministic_boundary TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS authority_scope TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS model_name TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS prompt_id TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS prompt_version TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS prompt_schema_version TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS review_status TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS fallback_used BOOLEAN",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS error TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS payload_json TEXT",
]


class CompanyMemoryReview(BaseModel):
    recommended_signal: Literal["BUY", "BUY_MORE", "HOLD", "WATCH", "SELL_PARTIAL", "SELL", "NO_ACTION"]
    confidence: float = Field(ge=0.0, le=1.0)
    conviction_score: float = Field(ge=0.0, le=100.0)
    summary: str = Field(min_length=10, max_length=900)
    thesis: str = Field(min_length=10, max_length=1400)
    risk_flags: list[str] = Field(default_factory=list, max_length=8)
    evidence_used: list[str] = Field(default_factory=list, max_length=12)
    wait_for: list[str] = Field(default_factory=list, max_length=8)
    deterministic_boundary: str = Field(
        default="Review input only. Deterministic action consolidation and execution safety checks remain authoritative.",
        max_length=500,
    )


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.company_memory_review",
            fallback_type="company_memory_json_ready_missing_check_failed",
            source="json_ready",
            severity="warn",
            reason="Company-memory review could not evaluate missingness while preparing JSON and kept the original value.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return value


def _records(df: pd.DataFrame, *, limit: int = 10) -> list[dict[str, Any]]:
    if df.empty:
        return []
    sample = df.head(max(0, int(limit))).copy()
    sample = sample.astype(object).where(pd.notna(sample), None)
    return [{key: _json_ready(value) for key, value in row.items()} for row in sample.to_dict(orient="records")]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.company_memory_review",
            fallback_type="company_memory_text_missing_check_failed",
            source="text",
            severity="warn",
            reason="Company-memory review could not evaluate missingness while normalizing text and kept string conversion fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    text = str(value).strip()
    return text or None


def _num(value: Any, default: float = 0.0) -> float:
    out = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(out) else float(out)


def _asof(value: Any = None) -> pd.Timestamp:
    ts = pd.to_datetime(value or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(ts):
        ts = pd.Timestamp.utcnow()
    return ts.normalize()


def table_exists(table_name: str) -> bool:
    try:
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
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.company_memory_review",
            fallback_type="company_memory_source_table_lookup_failed",
            source=table_name,
            severity="warn",
            reason="Company-memory context skipped a source because table existence lookup failed.",
            error=exc,
        )
        return False
    return not df.empty


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=COMPANY_MEMORY_SCHEMA_MIGRATION_ID,
        description="Create review-only company memory signal table.",
        statements=COMPANY_MEMORY_SCHEMA_STATEMENTS,
        metadata={"tables": [TABLE_NAME], "authority_scope": "review_input_only"},
    )


def load_review_symbols(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, limit: int = DEFAULT_MAX_SYMBOLS) -> list[str]:
    explicit = sorted({str(symbol).strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()})
    if explicit:
        return explicit[: max(1, int(limit))]

    frames: list[pd.DataFrame] = []
    sources = [
        (ACTIONS_TABLE, "asof_date"),
        (EVENT_POLICY_TABLE, "asof_date"),
        (CANDIDATES_TABLE, "asof_date"),
        (WATCHLIST_TABLE, "asof_date"),
    ]
    for table_name, date_col in sources:
        if not table_exists(table_name):
            continue
        try:
            frames.append(
                sql_to_df(
                    f"""
                    SELECT DISTINCT UPPER(TRIM(symbol)) AS symbol
                    FROM {table_name}
                    WHERE symbol IS NOT NULL
                      AND {date_col} <= %(asof_date)s
                    ORDER BY symbol
                    LIMIT %(limit)s
                    """,
                    params={"asof_date": asof_date, "limit": max(1, int(limit) * 4)},
                    retries=2,
                    statement_timeout_ms=10000,
                )
            )
        except Exception as exc:
            record_local_fallback_event(
                module="advisory.company_memory_review",
                fallback_type="company_memory_review_symbols_load_failed",
                source=table_name,
                severity="warn",
                reason="Company-memory review skipped a candidate-symbol source because symbol discovery failed.",
                error=exc,
                metadata={
                    "date_column": date_col,
                    "asof_date": None if pd.isna(asof_date) else pd.Timestamp(asof_date).isoformat(),
                    "limit": max(1, int(limit) * 4),
                },
            )
            continue
    if not frames:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for frame in frames:
        if frame.empty:
            continue
        for symbol in frame["symbol"].dropna().astype(str).tolist():
            clean = symbol.strip().upper()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            out.append(clean)
            if len(out) >= max(1, int(limit)):
                return out
    return out


def _load_table_rows(
    table_name: str,
    *,
    symbol: str,
    asof_date: pd.Timestamp,
    date_column: str,
    columns: list[str],
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = 8,
) -> pd.DataFrame:
    if not table_exists(table_name):
        return pd.DataFrame()
    start_date = asof_date - pd.Timedelta(days=max(1, int(lookback_days)))
    column_sql = ", ".join(columns)
    try:
        return sql_to_df(
            f"""
            SELECT {column_sql}
            FROM {table_name}
            WHERE UPPER(TRIM(symbol)) = %(symbol)s
              AND {date_column} <= %(asof_date)s
              AND {date_column} >= %(start_date)s
            ORDER BY {date_column} DESC
            LIMIT %(limit)s
            """,
            params={"symbol": symbol.upper(), "asof_date": asof_date, "start_date": start_date, "limit": max(1, int(limit))},
            retries=2,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.company_memory_review",
            fallback_type="company_memory_source_rows_load_failed",
            source=table_name,
            severity="warn",
            symbol=symbol.upper(),
            reason="Company-memory context skipped source rows because source loading failed.",
            error=exc,
            metadata={
                "date_column": date_column,
                "lookback_days": int(lookback_days),
                "limit": max(1, int(limit)),
            },
        )
        return pd.DataFrame()


def _load_wait_signal_rows(
    *,
    symbol: str,
    asof_date: pd.Timestamp,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = 8,
) -> pd.DataFrame:
    if not table_exists(WAIT_SIGNALS_TABLE):
        return pd.DataFrame()
    start_date = asof_date - pd.Timedelta(days=max(1, int(lookback_days)))
    try:
        return sql_to_df(
            f"""
            SELECT
                created_at,
                signal_id,
                status,
                signal_type,
                condition_summary,
                operator_summary,
                wait_question,
                valid_until
            FROM {WAIT_SIGNALS_TABLE}
            WHERE UPPER(TRIM(symbol)) = %(symbol)s
              AND created_at <= %(asof_date)s
              AND created_at >= %(start_date)s
              AND (valid_until IS NULL OR valid_until >= %(asof_date)s)
            ORDER BY created_at DESC
            LIMIT %(limit)s
            """,
            params={"symbol": symbol.upper(), "asof_date": asof_date, "start_date": start_date, "limit": max(1, int(limit))},
            retries=2,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.company_memory_review",
            fallback_type="company_memory_wait_signals_load_failed",
            source=WAIT_SIGNALS_TABLE,
            severity="warn",
            symbol=symbol.upper(),
            reason="Company-memory context skipped wait-signal rows because source loading failed.",
            error=exc,
            metadata={
                "lookback_days": int(lookback_days),
                "limit": max(1, int(limit)),
            },
        )
        return pd.DataFrame()


def load_company_memory_context(
    symbol: str,
    *,
    asof_date: pd.Timestamp,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, Any]:
    symbol = symbol.strip().upper()
    context: dict[str, Any] = {"symbol": symbol, "asof_date": asof_date.isoformat(), "lookback_days": int(lookback_days)}

    context["technical"] = _records(
        _load_table_rows(
            TECHNICAL_TABLE,
            symbol=symbol,
            asof_date=asof_date,
            date_column="asof_date",
            columns=[
                "asof_date",
                "adj_close",
                "dma_20",
                "dma_50",
                "dma_200",
                "avg_traded_value_20d",
                "rs_vs_benchmark",
                "rs_vs_sector",
                "dist_52w_high",
                "breakout_extension_pct",
                "sector_code",
            ],
            lookback_days=lookback_days,
            limit=1,
        )
    )
    context["candidates"] = _records(
        _load_table_rows(
            CANDIDATES_TABLE,
            symbol=symbol,
            asof_date=asof_date,
            date_column="asof_date",
            columns=[
                "asof_date",
                "setup_id",
                "setup_name",
                "candidate_state",
                "technical_state",
                "technical_trigger_type",
                "technical_trigger_note",
                "technical_score",
                "setup_score",
                "watch_reason_detail",
                "invalidation_price",
                "entry_note",
            ],
            lookback_days=lookback_days,
            limit=5,
        )
    )
    context["announcement_evidence"] = _records(
        _load_table_rows(
            ANNOUNCEMENT_EVIDENCE_TABLE,
            symbol=symbol,
            asof_date=asof_date + pd.Timedelta(days=1),
            date_column="published_on",
            columns=[
                "published_on",
                "unique_id",
                "subject",
                "filed_under_category",
                "source_reliability",
                "event_class",
                "direction",
                "materiality",
                "confidence",
                "verdict",
                "evidence_summary",
                "what_happened",
                "rationale",
            ],
            lookback_days=lookback_days,
            limit=8,
        )
    )
    context["bhavcopy_evidence"] = _records(
        _load_table_rows(
            BHAVCOPY_EVIDENCE_TABLE,
            symbol=symbol,
            asof_date=asof_date,
            date_column="asof_date",
            columns=[
                "asof_date",
                "close",
                "daily_return",
                "turnover_value_inr",
                "avg_turnover_value_20d",
                "deal_net_value_inr",
                "short_selling_quantity",
                "circuit_hit_count",
                "deal_pressure",
                "evidence_score",
                "evidence_summary",
            ],
            lookback_days=lookback_days,
            limit=10,
        )
    )
    context["event_policy"] = _records(
        _load_table_rows(
            EVENT_POLICY_TABLE,
            symbol=symbol,
            asof_date=asof_date + pd.Timedelta(days=1),
            date_column="asof_date",
            columns=[
                "asof_date",
                "published_on",
                "unique_id",
                "action_type",
                "policy_class",
                "event_class",
                "policy_score",
                "confidence",
                "action_reason",
                "llm_operator_summary",
                "llm_possible_action",
                "llm_event_to_wait_for",
            ],
            lookback_days=lookback_days,
            limit=8,
        )
    )
    context["actions"] = _records(
        _load_table_rows(
            ACTIONS_TABLE,
            symbol=symbol,
            asof_date=asof_date,
            date_column="asof_date",
            columns=[
                "asof_date",
                "action_code",
                "action_source",
                "action_reason",
                "recommended_stop_price",
                "recommended_target_price",
                "expected_horizon_days",
                "invest_score_pct",
                "reason_contract_status",
            ],
            lookback_days=lookback_days,
            limit=5,
        )
    )
    context["wait_signals"] = _records(
        _load_wait_signal_rows(
            symbol=symbol,
            asof_date=asof_date + pd.Timedelta(days=1),
            lookback_days=lookback_days,
            limit=8,
        )
    )
    context["evidence_source_contract"] = build_evidence_source_contract(context)
    return context


def build_evidence_source_contract(context: dict[str, Any]) -> dict[str, Any]:
    sources = {
        "announcement_evidence": {
            "source_table": ANNOUNCEMENT_EVIDENCE_TABLE,
            "purpose": "compact_event_context",
            "required_for_confident_upgrade": True,
        },
        "bhavcopy_evidence": {
            "source_table": BHAVCOPY_EVIDENCE_TABLE,
            "purpose": "compact_market_participation_context",
            "required_for_confident_upgrade": True,
        },
        "event_policy": {
            "source_table": EVENT_POLICY_TABLE,
            "purpose": "deterministic_event_policy_context",
            "required_for_confident_upgrade": False,
        },
        "technical": {
            "source_table": TECHNICAL_TABLE,
            "purpose": "technical_state_context",
            "required_for_confident_upgrade": True,
        },
        "actions": {
            "source_table": ACTIONS_TABLE,
            "purpose": "latest_consolidated_action_context",
            "required_for_confident_upgrade": False,
        },
        "wait_signals": {
            "source_table": WAIT_SIGNALS_TABLE,
            "purpose": "operator_wait_condition_context",
            "required_for_confident_upgrade": False,
        },
    }
    rows: list[dict[str, Any]] = []
    missing_required: list[str] = []
    for key, metadata in sources.items():
        count = len(context.get(key) or [])
        status = "present" if count > 0 else "missing"
        row = {
            "source": key,
            "source_table": metadata["source_table"],
            "purpose": metadata["purpose"],
            "row_count": int(count),
            "status": status,
            "required_for_confident_upgrade": bool(metadata["required_for_confident_upgrade"]),
        }
        rows.append(row)
        if status == "missing" and row["required_for_confident_upgrade"]:
            missing_required.append(key)
    return {
        "schema_version": 1,
        "authority_scope": "review_input_only",
        "uses_compact_evidence": True,
        "raw_announcement_scan_allowed": False,
        "raw_bhavcopy_scan_allowed": False,
        "sources": rows,
        "missing_required_sources": missing_required,
        "coverage_status": "complete" if not missing_required else "partial",
        "operator_note": (
            "Company-memory review used compact point-in-time evidence stores. "
            "Missing required sources should keep upgrades conservative and review-only."
        ),
    }


def _has_positive_event(context: dict[str, Any]) -> bool:
    for row in context.get("announcement_evidence") or []:
        if str(row.get("direction") or "").lower() == "positive" and _num(row.get("confidence")) >= 0.55:
            return True
    for row in context.get("event_policy") or []:
        if str(row.get("action_type") or "").upper() in {"BUY_WATCH", "NO_ACTION"} and _num(row.get("policy_score")) > 0:
            return True
    return False


def _has_negative_event(context: dict[str, Any]) -> bool:
    for row in context.get("announcement_evidence") or []:
        if str(row.get("direction") or "").lower() == "negative" and _num(row.get("confidence")) >= 0.55:
            return True
    for row in context.get("event_policy") or []:
        if str(row.get("action_type") or "").upper() in {"REDUCE_EXPOSURE_REVIEW"}:
            return True
    return False


def deterministic_review(context: dict[str, Any]) -> CompanyMemoryReview:
    latest_action = (context.get("actions") or [{}])[0]
    latest_candidate = (context.get("candidates") or [{}])[0]
    latest_technical = (context.get("technical") or [{}])[0]
    latest_bhav = (context.get("bhavcopy_evidence") or [{}])[0]
    action_code = str(latest_action.get("action_code") or "").upper()
    technical_score = max(_num(latest_candidate.get("technical_score")), _num(latest_candidate.get("setup_score")))
    rs_benchmark = _num(latest_technical.get("rs_vs_benchmark"))
    deal_pressure = str(latest_bhav.get("deal_pressure") or "neutral")
    positive_event = _has_positive_event(context)
    negative_event = _has_negative_event(context)

    signal = "NO_ACTION"
    confidence = 0.35
    conviction = max(0.0, min(100.0, technical_score))
    evidence: list[str] = []
    risks: list[str] = []
    wait_for: list[str] = []
    source_contract = context.get("evidence_source_contract") if isinstance(context.get("evidence_source_contract"), dict) else {}
    missing_required_sources = [str(item) for item in source_contract.get("missing_required_sources") or []]

    if action_code in {"SELL", "PARTIAL_SELL", "BUY", "BUY_MORE", "HOLD", "WATCH"}:
        signal = "SELL_PARTIAL" if action_code == "PARTIAL_SELL" else action_code
        evidence.append(f"latest consolidated action is {action_code}")
        confidence = 0.55
    if negative_event:
        signal = "SELL_PARTIAL" if signal in {"BUY", "BUY_MORE", "HOLD"} else "WATCH"
        risks.append("recent negative event-policy or announcement evidence needs review")
        confidence = max(confidence, 0.60)
    elif technical_score >= 78 and positive_event:
        signal = "BUY" if signal in {"NO_ACTION", "WATCH", "HOLD"} else signal
        evidence.append("technical score is strong and recent event evidence is positive")
        confidence = max(confidence, 0.65)
    elif technical_score >= 65:
        signal = "WATCH" if signal == "NO_ACTION" else signal
        evidence.append("technical score is constructive but not enough for an independent buy")
        wait_for.append("wait for confirmed breakout/retest or stronger event confirmation")
        confidence = max(confidence, 0.50)

    if deal_pressure == "distribution_or_pressure":
        risks.append("bhavcopy evidence shows distribution or short-selling pressure")
        if signal in {"BUY", "BUY_MORE"}:
            signal = "WATCH"
    elif deal_pressure == "accumulation":
        evidence.append("bhavcopy evidence shows accumulation pressure")
    if rs_benchmark < -0.05:
        risks.append("relative strength versus benchmark is weak")
    if not context.get("announcement_evidence"):
        wait_for.append("wait for fresh company-specific announcement/news evidence before upgrading confidence")
    if missing_required_sources:
        risks.append(f"company-memory evidence coverage is partial; missing {', '.join(missing_required_sources)}")

    summary = f"{context['symbol']} memory review suggests {signal}; confidence {confidence:.0%}."
    thesis = " | ".join(evidence or ["No strong compact company-memory signal was found; keep deterministic policy authoritative."])
    return CompanyMemoryReview(
        recommended_signal=signal,  # type: ignore[arg-type]
        confidence=round(min(1.0, confidence), 4),
        conviction_score=round(conviction, 2),
        summary=summary,
        thesis=thesis,
        risk_flags=risks[:8],
        evidence_used=evidence[:12],
        wait_for=wait_for[:8],
    )


def llm_review(context: dict[str, Any], *, model: str) -> CompanyMemoryReview:
    prompt = (
        "You are reviewing compact point-in-time company memory for an Indian equity. "
        "Return a review input only, not an executable trading instruction. "
        "The deterministic action engine remains authoritative. "
        "Use BUY/BUY_MORE/HOLD/WATCH/SELL_PARTIAL/SELL/NO_ACTION only when justified by the evidence.\n\n"
        f"Company memory JSON:\n{json.dumps(_json_ready(context), ensure_ascii=False, indent=2, default=str)}"
    )
    return run_codex_structured(prompt, response_model=CompanyMemoryReview, model=model, system_prompt="Return only the requested structured company-memory review.")


def build_review_row(
    *,
    review_date: pd.Timestamp,
    symbol: str,
    context: dict[str, Any],
    model: str,
    use_llm: bool,
) -> dict[str, Any]:
    fallback_used = False
    error: str | None = None
    review_status = "completed"
    try:
        review = llm_review(context, model=model) if use_llm else deterministic_review(context)
    except Exception as exc:
        fallback_used = True
        error = f"{type(exc).__name__}: {exc}"
        review_status = "fallback_completed"
        record_fallback_event(
            module="advisory.company_memory_review",
            source="company_memory_review",
            fallback_type="llm_deterministic_fallback",
            severity="warn",
            symbol=context.get("symbol"),
            reason="Company-memory LLM review failed; deterministic review was used.",
            deterministic_fallback=True,
            error=exc,
            metadata={"model": model},
        )
        review = deterministic_review(context)

    return {
        "review_date": review_date,
        "symbol": symbol.upper(),
        "recommended_signal": review.recommended_signal,
        "confidence": float(review.confidence),
        "conviction_score": float(review.conviction_score),
        "summary": review.summary,
        "thesis": review.thesis,
        "risk_flags_json": json_dumps(review.risk_flags),
        "evidence_used_json": json_dumps(review.evidence_used),
        "wait_for_json": json_dumps(review.wait_for),
        "deterministic_boundary": review.deterministic_boundary,
        "authority_scope": "review_input_only",
        "model_name": model if use_llm else "deterministic_company_memory_v1",
        "prompt_id": PROMPT_ID,
        "prompt_version": PROMPT_VERSION,
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "review_status": review_status,
        "fallback_used": bool(fallback_used),
        "error": error,
        "payload_json": json_dumps(context),
        "load_ts": pd.Timestamp.utcnow(),
    }


def build_company_memory_reviews(
    *,
    asof_date: Any = None,
    symbols: list[str] | None = None,
    limit: int = DEFAULT_MAX_SYMBOLS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    model: str = DEFAULT_MODEL,
    use_llm: bool = LLM_ENABLED,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    review_date = _asof(asof_date)
    review_symbols = load_review_symbols(asof_date=review_date, symbols=symbols, limit=limit)
    rows: list[dict[str, Any]] = []
    for symbol in review_symbols:
        context = load_company_memory_context(symbol, asof_date=review_date, lookback_days=lookback_days)
        rows.append(build_review_row(review_date=review_date, symbol=symbol, context=context, model=model, use_llm=use_llm))
    df = pd.DataFrame(rows)
    meta = {
        "status": "ok",
        "review_date": review_date.isoformat(),
        "symbol_count": int(len(review_symbols)),
        "review_rows": int(len(df)),
        "llm_enabled": bool(use_llm),
        "model": model if use_llm else "deterministic_company_memory_v1",
        "authority_scope": "review_input_only",
        "table": TABLE_NAME,
    }
    return df, meta


def persist_company_memory_reviews(reviews: pd.DataFrame) -> None:
    ensure_table()
    if reviews.empty:
        return
    out = reviews.copy()
    out["review_date"] = pd.to_datetime(out["review_date"], utc=True, errors="coerce")
    out["load_ts"] = pd.to_datetime(out["load_ts"], utc=True, errors="coerce")
    for column in ["confidence", "conviction_score"]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out["fallback_used"] = out["fallback_used"].astype("boolean")
    upsert_to_db(out, TABLE_NAME, unique_keys=["review_date", "symbol"])


def summarize(reviews: pd.DataFrame, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        **meta,
        "signal_counts": reviews["recommended_signal"].value_counts(dropna=False).to_dict() if not reviews.empty else {},
        "sample": _records(reviews, limit=10),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build review-only company-memory signal summaries from compact evidence.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Review date, defaults to today UTC")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--limit", type=int, default=DEFAULT_MAX_SYMBOLS)
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--llm", action="store_true", help="Use Codex structured review instead of deterministic V1")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reviews, meta = build_company_memory_reviews(
        asof_date=args.date,
        symbols=args.symbols,
        limit=int(args.limit),
        lookback_days=int(args.lookback_days),
        model=str(args.model),
        use_llm=bool(args.llm),
    )
    if not args.dry_run:
        persist_company_memory_reviews(reviews)
    result = summarize(reviews, meta)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
