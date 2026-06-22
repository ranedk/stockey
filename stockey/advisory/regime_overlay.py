from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any, Literal

import pandas as pd
from environs import Env
from pydantic import BaseModel, Field

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.prompt_registry import prompt_version as registry_prompt_version
from advisory.prompt_registry import response_schema_version
from utils.codex_cli import CodexCLIError, run_codex_structured
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


env = Env()
env.read_env()

TABLE_NAME = "advisory_regime_overlay_proposals"
DECISIONS_TABLE = "advisory_regime_overlay_decisions"
REGIME_TABLE = "advisory_market_regime"
MARKET_CONTEXT_TABLE = "advisory_market_context_summary_daily"
MACRO_FEATURES_TABLE = "advisory_macro_features_daily"
ANNOUNCEMENT_EVIDENCE_TABLE = "advisory_announcement_evidence"
NEWS_EVENTS_TABLE = "advisory_news_events"
ET_RSS_TABLE = "economictimes_rss_items"

PROMPT_ID = "regime_overlay_proposal"
PROMPT_VERSION = registry_prompt_version(PROMPT_ID)
PROMPT_SCHEMA_VERSION = response_schema_version(PROMPT_ID)
DEFAULT_MODEL = env("REGIME_OVERLAY_MODEL", default=env("CODEX_CLI_MODEL", default="gpt-5.4-mini"))
LLM_ENABLED = env.bool("REGIME_OVERLAY_LLM_ENABLED", default=False)
DEFAULT_LOOKBACK_DAYS = env.int("REGIME_OVERLAY_LOOKBACK_DAYS", default=7)

REGIME_OVERLAY_SCHEMA_MIGRATION_ID = "20260618_advisory_regime_overlay_proposals_base"
REGIME_OVERLAY_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        asof_date TIMESTAMPTZ NOT NULL,
        proposal_id TEXT NOT NULL,
        base_regime TEXT,
        proposed_regime TEXT NOT NULL,
        regime_family TEXT,
        confidence DOUBLE PRECISION,
        recommended_bias TEXT,
        summary TEXT,
        rationale TEXT,
        evidence_json TEXT,
        rule_suggestions_json TEXT,
        positioning_policy_json TEXT,
        operator_questions_json TEXT,
        expires_on TIMESTAMPTZ,
        authority_scope TEXT,
        production_status TEXT,
        prompt_id TEXT,
        prompt_version TEXT,
        prompt_schema_version TEXT,
        model_name TEXT,
        fallback_used BOOLEAN,
        error TEXT,
        raw_response_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date, proposal_id)
    )
    """,
    *[
        f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {column} {sql_type}"
        for column, sql_type in {
            "base_regime": "TEXT",
            "proposed_regime": "TEXT",
            "regime_family": "TEXT",
            "confidence": "DOUBLE PRECISION",
            "recommended_bias": "TEXT",
            "summary": "TEXT",
            "rationale": "TEXT",
            "evidence_json": "TEXT",
            "rule_suggestions_json": "TEXT",
            "positioning_policy_json": "TEXT",
            "operator_questions_json": "TEXT",
            "expires_on": "TIMESTAMPTZ",
            "authority_scope": "TEXT",
            "production_status": "TEXT",
            "prompt_id": "TEXT",
            "prompt_version": "TEXT",
            "prompt_schema_version": "TEXT",
            "model_name": "TEXT",
            "fallback_used": "BOOLEAN",
            "error": "TEXT",
            "raw_response_json": "TEXT",
            "load_ts": "TIMESTAMPTZ",
        }.items()
    ],
]
REGIME_OVERLAY_DECISIONS_SCHEMA_MIGRATION_ID = "20260618_advisory_regime_overlay_decisions_base"
REGIME_OVERLAY_DECISIONS_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {DECISIONS_TABLE} (
        decision_id TEXT NOT NULL,
        decided_at TIMESTAMPTZ NOT NULL,
        asof_date TIMESTAMPTZ NOT NULL,
        proposal_id TEXT NOT NULL,
        proposed_regime TEXT,
        decision TEXT NOT NULL,
        decision_reason TEXT,
        operator_id TEXT,
        item_snapshot_json TEXT,
        decision_effect_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (decision_id)
    )
    """,
    f"ALTER TABLE {DECISIONS_TABLE} ADD COLUMN IF NOT EXISTS proposed_regime TEXT",
    f"ALTER TABLE {DECISIONS_TABLE} ADD COLUMN IF NOT EXISTS decision_reason TEXT",
    f"ALTER TABLE {DECISIONS_TABLE} ADD COLUMN IF NOT EXISTS operator_id TEXT",
    f"ALTER TABLE {DECISIONS_TABLE} ADD COLUMN IF NOT EXISTS item_snapshot_json TEXT",
    f"ALTER TABLE {DECISIONS_TABLE} ADD COLUMN IF NOT EXISTS decision_effect_json TEXT",
]
ALLOWED_DECISIONS = {"approve_for_testing", "reject", "needs_more_evidence", "promote_to_review_rule"}
DECISION_STATUS = {
    "approve_for_testing": "approved_for_testing",
    "reject": "rejected",
    "needs_more_evidence": "needs_more_evidence",
    "promote_to_review_rule": "promoted_review_rule",
}


class RegimeRuleSuggestion(BaseModel):
    rule_name: str = Field(min_length=4, max_length=120)
    condition: str = Field(min_length=10, max_length=500)
    expected_effect: str = Field(min_length=10, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)
    review_note: str = Field(default="", max_length=500)


class RegimeOverlayProposal(BaseModel):
    proposed_regime: str = Field(min_length=4, max_length=120)
    regime_family: Literal[
        "RISK_ON",
        "RISK_OFF",
        "TRANSITION",
        "SHOCK",
        "LIQUIDITY",
        "COMMODITY",
        "RATE",
        "GEOPOLITICAL",
        "EARNINGS_BREADTH",
        "CUSTOM",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_bias: Literal["risk_on", "neutral", "reduce_risk", "defensive_rotation", "cash_wait", "custom"]
    summary: str = Field(min_length=20, max_length=900)
    rationale: str = Field(min_length=20, max_length=1600)
    evidence_used: list[str] = Field(default_factory=list, max_length=14)
    rule_suggestions: list[RegimeRuleSuggestion] = Field(default_factory=list, max_length=8)
    positioning_policy: dict[str, Any] = Field(default_factory=dict)
    operator_questions: list[str] = Field(default_factory=list, max_length=8)
    expiry_days: int = Field(default=5, ge=1, le=30)


def _json_dumps(value: Any) -> str:
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
    except (TypeError, ValueError) as exc:
        record_local_fallback_event(
            module="advisory.regime_overlay",
            fallback_type="regime_overlay_json_ready_missing_check_failed",
            source="json_ready",
            severity="warn",
            reason="Regime overlay could not evaluate missingness for a JSON value and kept the original value.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return value


def _safe_float(value: Any, default: float = 0.0) -> float:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return default
    return float(numeric)


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError) as exc:
        record_local_fallback_event(
            module="advisory.regime_overlay",
            fallback_type="regime_overlay_text_missing_check_failed",
            source="text",
            severity="warn",
            reason="Regime overlay could not evaluate missingness for a text value and converted it with str().",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return str(value)


def _normalize_asof(value: Any = None) -> pd.Timestamp:
    if value is None:
        return pd.Timestamp.utcnow().normalize()
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"Invalid asof date: {value}")
    return ts.normalize()


def table_exists(table_name: str) -> bool:
    schema_name, base_table_name = table_name.split(".", 1) if "." in table_name else ("public", table_name)
    df = sql_to_df(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
        ) AS exists
        """,
        params=(schema_name, base_table_name),
    )
    return bool(df.iloc[0]["exists"]) if not df.empty else False


def table_columns(table_name: str) -> set[str]:
    schema_name, base_table_name = table_name.split(".", 1) if "." in table_name else ("public", table_name)
    df = sql_to_df(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        params=(schema_name, base_table_name),
        retries=2,
    )
    return set(df["column_name"].astype(str).tolist()) if not df.empty and "column_name" in df.columns else set()


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=REGIME_OVERLAY_SCHEMA_MIGRATION_ID,
        statements=REGIME_OVERLAY_SCHEMA_STATEMENTS,
        owner="advisory.regime_overlay",
        description="Create review-only LLM/deterministic regime overlay proposal table.",
        metadata={"tables": [TABLE_NAME], "workflow": "regime_overlay", "authority_scope": "review_input_only"},
    )


def ensure_decision_table() -> None:
    apply_schema_migration(
        migration_id=REGIME_OVERLAY_DECISIONS_SCHEMA_MIGRATION_ID,
        statements=REGIME_OVERLAY_DECISIONS_SCHEMA_STATEMENTS,
        owner="advisory.regime_overlay",
        description="Create operator decision table for review-only regime overlay proposals.",
        metadata={
            "tables": [DECISIONS_TABLE],
            "source_tables": [TABLE_NAME],
            "workflow": "regime_overlay_review",
            "authority_scope": "review_input_only",
        },
    )


def ensure_review_tables() -> None:
    ensure_table()
    ensure_decision_table()


def _latest_row(table_name: str, date_column: str, asof_date: pd.Timestamp, *, columns: list[str] | None = None) -> dict[str, Any]:
    if not table_exists(table_name):
        return {}
    selected = ", ".join(columns or ["*"])
    df = sql_to_df(
        f"""
        SELECT {selected}
        FROM {table_name}
        WHERE {date_column} <= %(asof_date)s
        ORDER BY {date_column} DESC
        LIMIT 1
        """,
        params={"asof_date": asof_date},
        retries=3,
    )
    return df.iloc[0].to_dict() if not df.empty else {}


def _load_recent_market_news(asof_date: pd.Timestamp, lookback_days: int) -> list[dict[str, Any]]:
    start = asof_date - pd.Timedelta(days=lookback_days)
    end = asof_date + pd.Timedelta(days=1)
    for table_name, title_col, desc_col, date_col in [
        (NEWS_EVENTS_TABLE, "subject", "concise_summary_text", "published_on"),
        (ET_RSS_TABLE, "title", "description", "published_on"),
    ]:
        if not table_exists(table_name):
            continue
        df = sql_to_df(
            f"""
            SELECT {date_col} AS published_on, {title_col} AS title, {desc_col} AS description
            FROM {table_name}
            WHERE {date_col} >= %(start)s
              AND {date_col} < %(end)s
            ORDER BY {date_col} DESC
            LIMIT 25
            """,
            params={"start": start, "end": end},
            retries=3,
        )
        if not df.empty:
            return [_json_ready(row) for row in df.to_dict(orient="records")]
    return []


def _load_event_counts(asof_date: pd.Timestamp, lookback_days: int) -> dict[str, Any]:
    if not table_exists(ANNOUNCEMENT_EVIDENCE_TABLE):
        return {}
    start = asof_date - pd.Timedelta(days=lookback_days)
    end = asof_date + pd.Timedelta(days=1)
    columns = table_columns(ANNOUNCEMENT_EVIDENCE_TABLE)
    filters = []
    if "announcement_storage_form" in columns:
        filters.append("COALESCE(announcement_storage_form, '') <> 'archived_raw_reference_only'")
    if "llm_review_ready" in columns:
        filters.append("COALESCE(llm_review_ready, TRUE) IS TRUE")
    taxonomy_filter_sql = f" AND {' AND '.join(filters)}" if filters else ""
    df = sql_to_df(
        f"""
        SELECT
            COALESCE(event_class, filed_under_category, 'UNKNOWN') AS event_class,
            COALESCE(direction, 'unknown') AS direction,
            COUNT(*) AS row_count,
            AVG(confidence) AS avg_confidence,
            AVG(materiality) AS avg_materiality
        FROM {ANNOUNCEMENT_EVIDENCE_TABLE}
        WHERE published_on >= %(start)s
          AND published_on < %(end)s
          {taxonomy_filter_sql}
        GROUP BY 1, 2
        ORDER BY row_count DESC
        LIMIT 20
        """,
        params={"start": start, "end": end},
        retries=3,
    )
    if df.empty:
        counts: list[dict[str, Any]] = []
    else:
        counts = [_json_ready(row) for row in df.to_dict(orient="records")]
    payload: dict[str, Any] = {
        "announcement_event_counts": counts,
        "announcement_taxonomy_filter_applied": bool(filters),
        "announcement_taxonomy_filter_columns": sorted(column for column in ["announcement_storage_form", "llm_review_ready"] if column in columns),
        "archive_only_rows_excluded": "announcement_storage_form" in columns,
    }
    return payload if counts or filters else {}


def load_regime_overlay_context(*, asof_date: Any = None, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict[str, Any]:
    effective_asof = _normalize_asof(asof_date)
    return {
        "asof_date": effective_asof,
        "base_regime": _latest_row(REGIME_TABLE, "asof_date", effective_asof),
        "market_context": _latest_row(MARKET_CONTEXT_TABLE, "asof_date", effective_asof),
        "macro_features": _latest_row(MACRO_FEATURES_TABLE, "asof_date", effective_asof),
        "recent_market_news": _load_recent_market_news(effective_asof, lookback_days),
        "event_counts": _load_event_counts(effective_asof, lookback_days),
        "lookback_days": int(lookback_days),
    }


def _news_text(context: dict[str, Any]) -> str:
    pieces: list[str] = []
    for row in context.get("recent_market_news") or []:
        pieces.append(f"{row.get('title') or ''} {row.get('description') or ''}")
    return " ".join(pieces).lower()


def deterministic_regime_overlay(context: dict[str, Any]) -> RegimeOverlayProposal:
    base = context.get("base_regime") or {}
    market = context.get("market_context") or {}
    macro = context.get("macro_features") or {}
    news_text = _news_text(context)
    base_regime = _text(base.get("regime_name")) or _text(market.get("regime_name")) or "UNKNOWN"
    risk_on_score = _safe_float(market.get("risk_on_score"), 0.0)
    risk_off_score = _safe_float(market.get("risk_off_score"), 0.0)
    macro_stress = _safe_float(base.get("macro_stress_score", macro.get("macro_stress_score")), 0.0)
    event_counts = context.get("event_counts") or {}
    has_deescalation = any(word in news_text for word in ["peace", "ceasefire", "truce", "de-escalat", "deescalat"])
    oil_relief = any(word in news_text for word in ["oil falls", "crude falls", "brent falls", "oil prices fall", "crude prices fall"])

    evidence = [
        f"Base deterministic regime is {base_regime}.",
        f"Market-context scores: risk_on={risk_on_score:.2f}, risk_off={risk_off_score:.2f}.",
        f"Macro stress score is {macro_stress:.2f}.",
    ]
    if has_deescalation:
        evidence.append("Recent market news includes de-escalation or peace-language.")
    if oil_relief:
        evidence.append("Recent market news includes oil/crude relief language.")
    if event_counts.get("announcement_event_counts"):
        evidence.append("Recent announcement evidence counts are available for cross-checking sector/company impact.")

    if base_regime in {"RISK_OFF", "SHOCK"} and (risk_off_score < 0.70 or has_deescalation or oil_relief):
        return RegimeOverlayProposal(
            proposed_regime="TRANSITION_RISK_OFF_TO_NEUTRAL",
            regime_family="TRANSITION",
            confidence=0.62 if has_deescalation or oil_relief else 0.52,
            recommended_bias="neutral",
            summary="Deterministic regime is still defensive, but breadth/news evidence suggests a transition watch rather than an automatic hard risk-off label.",
            rationale="Keep the base regime authoritative for now, but ask the operator to review whether recent de-escalation, oil relief, or improving breadth invalidates the stale risk-off interpretation.",
            evidence_used=evidence,
            rule_suggestions=[
                RegimeRuleSuggestion(
                    rule_name="risk_off_transition_review",
                    condition="Base regime is RISK_OFF/SHOCK while risk_off_score drops below 0.70 or credible de-escalation/oil-relief news appears.",
                    expected_effect="Move positive actions from hard block to manual transition review until market breadth confirms.",
                    confidence=0.62,
                    review_note="Do not promote until validated against realised follow-through and drawdown.",
                )
            ],
            positioning_policy={
                "positive_actions": "manual_review_until_promoted",
                "new_risk": "small_size_only_after_technical_confirmation",
                "exits": "keep active; do not loosen stops automatically",
            },
            operator_questions=[
                "Has benchmark breadth improved for at least two sessions after the news?",
                "Did crude/INR/volatility confirm the de-escalation rather than reverse?",
                "Are sector leaders producing clean BUY_TRIGGERED technical states?",
            ],
            expiry_days=5,
        )

    if macro_stress >= 0.60 or risk_off_score >= 0.75:
        return RegimeOverlayProposal(
            proposed_regime="MACRO_STRESS_DEFENSIVE",
            regime_family="RISK_OFF",
            confidence=0.70,
            recommended_bias="reduce_risk",
            summary="Macro or breadth stress remains elevated; dynamic overlay keeps the operator focused on risk reduction.",
            rationale="The deterministic regime and market-context risk-off score are aligned enough that positive actions should remain gated unless there is strong technical confirmation.",
            evidence_used=evidence,
            rule_suggestions=[
                RegimeRuleSuggestion(
                    rule_name="macro_stress_positive_action_gate",
                    condition="macro_stress_score >= 0.60 or market-context risk_off_score >= 0.75.",
                    expected_effect="Require higher technical conviction and explicit operator approval for new long exposure.",
                    confidence=0.70,
                )
            ],
            positioning_policy={"positive_actions": "gate", "position_size": "reduced", "cash_bias": "higher"},
            operator_questions=["Which macro input is driving stress, and is it fresh enough to trust?"],
            expiry_days=3,
        )

    return RegimeOverlayProposal(
        proposed_regime="BASE_REGIME_CONFIRMED",
        regime_family="CUSTOM",
        confidence=0.50,
        recommended_bias="neutral",
        summary="No strong dynamic overlay is warranted from the current compact macro/news evidence.",
        rationale="Use the deterministic regime and market-context summary as the authoritative state until stronger macro, news, or breadth evidence appears.",
        evidence_used=evidence,
        rule_suggestions=[],
        positioning_policy={"positive_actions": "use_existing_policy", "execution_authority": "none"},
        operator_questions=["Is there any off-system macro or geopolitical information that materially changes this view?"],
        expiry_days=3,
    )


def build_regime_overlay_prompt(context: dict[str, Any]) -> str:
    compact_context = _json_ready(context)
    return (
        "You are proposing review-only market regime overlays for an Indian equities advisory system.\n"
        "Do not recommend broker execution. Do not override deterministic policy. Your job is to propose "
        "a named regime layer and candidate rules that a human can review/promote later.\n\n"
        "Use new regime names when the supplied macro/news evidence does not fit the existing base regime, "
        "but keep them falsifiable, time-bound, and operational.\n\n"
        f"Context JSON:\n{json.dumps(compact_context, indent=2, ensure_ascii=False, default=str)}"
    )


def propose_regime_overlay(
    *,
    context: dict[str, Any],
    use_llm: bool = LLM_ENABLED,
    model: str = DEFAULT_MODEL,
) -> tuple[RegimeOverlayProposal, bool, str | None, dict[str, Any] | None]:
    fallback = deterministic_regime_overlay(context)
    if not use_llm:
        return fallback, True, None, fallback.model_dump()
    try:
        proposal = run_codex_structured(
            build_regime_overlay_prompt(context),
            response_model=RegimeOverlayProposal,
            model=model,
            system_prompt=(
                "Return a cautious review-only regime proposal. Never create broker-executable instructions. "
                "Prefer abstention/transition review when evidence is mixed."
            ),
            max_attempts=2,
        )
        return proposal, False, None, proposal.model_dump()
    except (CodexCLIError, Exception) as exc:
        record_local_fallback_event(
            module="advisory.regime_overlay",
            source=PROMPT_ID,
            fallback_type="regime_overlay_llm_failed",
            severity="warn",
            reason="Regime overlay LLM proposal failed; deterministic review-only fallback will be used.",
            error=exc,
            metadata={"model": model, "asof_date": str(context.get("asof_date"))},
        )
        return fallback, True, f"{type(exc).__name__}: {exc}", fallback.model_dump()


def proposal_id_for(asof_date: pd.Timestamp, proposal: RegimeOverlayProposal) -> str:
    payload = f"{asof_date.date().isoformat()}|{proposal.proposed_regime}|{proposal.regime_family}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"REGIME_OVERLAY_{digest}"


def proposal_to_row(
    *,
    asof_date: pd.Timestamp,
    base_regime: str,
    proposal: RegimeOverlayProposal,
    fallback_used: bool,
    error: str | None,
    raw_response: dict[str, Any] | None,
    model: str,
) -> dict[str, Any]:
    return {
        "asof_date": asof_date,
        "proposal_id": proposal_id_for(asof_date, proposal),
        "base_regime": base_regime,
        "proposed_regime": proposal.proposed_regime,
        "regime_family": proposal.regime_family,
        "confidence": float(proposal.confidence),
        "recommended_bias": proposal.recommended_bias,
        "summary": proposal.summary,
        "rationale": proposal.rationale,
        "evidence_json": _json_dumps(proposal.evidence_used),
        "rule_suggestions_json": _json_dumps([item.model_dump() for item in proposal.rule_suggestions]),
        "positioning_policy_json": _json_dumps(proposal.positioning_policy),
        "operator_questions_json": _json_dumps(proposal.operator_questions),
        "expires_on": asof_date + pd.Timedelta(days=int(proposal.expiry_days)),
        "authority_scope": "review_input_only",
        "production_status": "proposed",
        "prompt_id": PROMPT_ID,
        "prompt_version": PROMPT_VERSION,
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "model_name": model,
        "fallback_used": bool(fallback_used),
        "error": error,
        "raw_response_json": _json_dumps(raw_response or {}),
        "load_ts": pd.Timestamp.utcnow(),
    }


def build_regime_overlay(
    *,
    asof_date: Any = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    use_llm: bool = LLM_ENABLED,
    model: str = DEFAULT_MODEL,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    context = load_regime_overlay_context(asof_date=asof_date, lookback_days=lookback_days)
    effective_asof = _normalize_asof(context["asof_date"])
    proposal, fallback_used, error, raw_response = propose_regime_overlay(context=context, use_llm=use_llm, model=model)
    base_regime = _text((context.get("base_regime") or {}).get("regime_name")) or _text(
        (context.get("market_context") or {}).get("regime_name")
    )
    row = proposal_to_row(
        asof_date=effective_asof,
        base_regime=base_regime,
        proposal=proposal,
        fallback_used=fallback_used,
        error=error,
        raw_response=raw_response,
        model=model,
    )
    meta = {
        "status": "ok",
        "table": TABLE_NAME,
        "asof_date": effective_asof.isoformat(),
        "lookback_days": int(lookback_days),
        "llm_enabled": bool(use_llm),
        "fallback_used": bool(fallback_used),
        "error": error,
        "authority_scope": "review_input_only",
        "production_status": "proposed",
    }
    return pd.DataFrame([row]), meta


def persist_regime_overlay(df: pd.DataFrame, *, rebuild: bool = False, asof_date: pd.Timestamp | None = None) -> None:
    ensure_table()
    if df.empty:
        return
    effective_asof = asof_date or pd.to_datetime(df["asof_date"].iloc[0], utc=True, errors="coerce")
    if rebuild and effective_asof is not None and not pd.isna(effective_asof):
        def _delete_existing() -> None:
            with db_session() as (_, cur):
                cur.execute(f"DELETE FROM {TABLE_NAME} WHERE asof_date = %s", (effective_asof,))

        execute_db_operation(_delete_existing, operation_name="regime_overlay:delete_rebuild")
    upsert_to_db(df, TABLE_NAME, unique_keys=["asof_date", "proposal_id"], timescaledb_column="asof_date")


def _json_loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        record_local_fallback_event(
            module="advisory.regime_overlay",
            fallback_type="regime_overlay_json_parse_failed",
            source="json_loads",
            severity="warn",
            reason="Regime overlay could not parse stored JSON and used the provided default.",
            error=exc,
            metadata={"value_type": type(value).__name__, "value_length": len(text), "value_excerpt": text[:240]},
        )
        return default


def decision_effect(decision: str) -> dict[str, Any]:
    normalized = str(decision or "").strip().lower()
    effects = {
        "approve_for_testing": {
            "proposal_status": "approved_for_testing",
            "meaning": "Keep this regime overlay available for research/testing review.",
            "changes_action_policy": False,
            "changes_portfolio": False,
            "submits_broker_order": False,
            "next_step": "Compare outcomes and manually decide whether a future config/rule change is justified.",
        },
        "reject": {
            "proposal_status": "rejected",
            "meaning": "Close this proposal as not useful for current review.",
            "changes_action_policy": False,
            "changes_portfolio": False,
            "submits_broker_order": False,
            "next_step": "No production behavior changes. The row remains audit evidence.",
        },
        "needs_more_evidence": {
            "proposal_status": "needs_more_evidence",
            "meaning": "Keep this proposal open but mark that more macro/news/breadth evidence is required.",
            "changes_action_policy": False,
            "changes_portfolio": False,
            "submits_broker_order": False,
            "next_step": "Recheck after more data arrives or after a fresh regime overlay run.",
        },
        "promote_to_review_rule": {
            "proposal_status": "promoted_review_rule",
            "meaning": "Promote the proposal to a reviewed rule candidate only.",
            "changes_action_policy": False,
            "changes_portfolio": False,
            "submits_broker_order": False,
            "next_step": "A separate reviewed config/rule implementation is still required before action consolidation can consume this.",
        },
    }
    if normalized not in effects:
        raise ValueError(f"decision must be one of {sorted(ALLOWED_DECISIONS)}")
    return effects[normalized]


def _normalize_proposal_row(row: dict[str, Any]) -> dict[str, Any]:
    out = _json_ready(row)
    for col in ["evidence_json", "rule_suggestions_json", "positioning_policy_json", "operator_questions_json", "raw_response_json"]:
        out[col.replace("_json", "")] = _json_loads(out.get(col), [] if col != "positioning_policy_json" and col != "raw_response_json" else {})
    if "latest_decision_effect_json" in out:
        out["latest_decision_effect"] = _json_loads(out.get("latest_decision_effect_json"), {})
    return out


def load_regime_overlay_reviews(*, limit: int = 25, status: str | None = None) -> list[dict[str, Any]]:
    ensure_review_tables()
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": max(1, int(limit))}
    if status and str(status).lower() != "all":
        clauses.append("p.production_status = %(status)s")
        params["status"] = str(status).strip()
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    df = sql_to_df(
        f"""
        SELECT
            p.*,
            d.decision AS latest_decision,
            d.decided_at AS latest_decided_at,
            d.decision_reason AS latest_decision_reason,
            d.operator_id AS latest_operator_id,
            d.decision_effect_json AS latest_decision_effect_json
        FROM {TABLE_NAME} p
        LEFT JOIN LATERAL (
            SELECT *
            FROM {DECISIONS_TABLE} d
            WHERE d.proposal_id = p.proposal_id
              AND d.asof_date = p.asof_date
            ORDER BY d.decided_at DESC
            LIMIT 1
        ) d ON TRUE
        {where_sql}
        ORDER BY p.asof_date DESC, p.load_ts DESC NULLS LAST, p.confidence DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params=params,
        retries=3,
    )
    return [_normalize_proposal_row(row) for row in df.to_dict(orient="records")]


def load_regime_overlay_decisions(*, limit: int = 25) -> list[dict[str, Any]]:
    ensure_decision_table()
    df = sql_to_df(
        f"""
        SELECT *
        FROM {DECISIONS_TABLE}
        ORDER BY decided_at DESC
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        retries=3,
    )
    rows = []
    for row in df.to_dict(orient="records"):
        ready = _json_ready(row)
        ready["item_snapshot"] = _json_loads(ready.get("item_snapshot_json"), {})
        ready["decision_effect"] = _json_loads(ready.get("decision_effect_json"), {})
        rows.append(ready)
    return rows


def record_regime_overlay_decision(
    *,
    asof_date: Any,
    proposal_id: str,
    decision: str,
    decision_reason: str | None = None,
    operator_id: str | None = None,
) -> dict[str, Any]:
    ensure_review_tables()
    effective_asof = _normalize_asof(asof_date)
    normalized_decision = str(decision or "").strip().lower()
    effect = decision_effect(normalized_decision)
    proposal_key = str(proposal_id or "").strip()
    if not proposal_key:
        raise ValueError("proposal_id is required")
    proposal_df = sql_to_df(
        f"""
        SELECT *
        FROM {TABLE_NAME}
        WHERE asof_date = %(asof_date)s
          AND proposal_id = %(proposal_id)s
        LIMIT 1
        """,
        params={"asof_date": effective_asof, "proposal_id": proposal_key},
        retries=3,
    )
    if proposal_df.empty:
        raise ValueError(f"Unknown regime overlay proposal: {proposal_key} at {effective_asof.date()}")
    proposal = _normalize_proposal_row(proposal_df.iloc[0].to_dict())
    decided_at = pd.Timestamp.utcnow()
    decision_id = hashlib.sha1(
        f"{proposal_key}|{effective_asof.isoformat()}|{normalized_decision}|{decided_at.isoformat()}".encode("utf-8")
    ).hexdigest()
    row = {
        "decision_id": decision_id,
        "decided_at": decided_at,
        "asof_date": effective_asof,
        "proposal_id": proposal_key,
        "proposed_regime": proposal.get("proposed_regime"),
        "decision": normalized_decision,
        "decision_reason": decision_reason,
        "operator_id": operator_id,
        "item_snapshot_json": _json_dumps(proposal),
        "decision_effect_json": _json_dumps(effect),
        "load_ts": decided_at,
    }
    upsert_to_db(pd.DataFrame([row]), DECISIONS_TABLE, unique_keys=["decision_id"])

    def _update_proposal_status() -> None:
        with db_session() as (_, cur):
            cur.execute(
                f"""
                UPDATE {TABLE_NAME}
                SET production_status = %s
                WHERE asof_date = %s
                  AND proposal_id = %s
                """,
                (DECISION_STATUS[normalized_decision], effective_asof, proposal_key),
            )

    execute_db_operation(_update_proposal_status, operation_name="regime_overlay:update_proposal_status")
    return {
        "status": "ok",
        "decided_at": decided_at.isoformat(),
        "decision": normalized_decision,
        "proposal_id": proposal_key,
        "asof_date": effective_asof.isoformat(),
        "proposal_status": DECISION_STATUS[normalized_decision],
        "decision_effect": effect,
        "proposal": proposal,
        "operator_boundary": {
            "authority_scope": "review_input_only",
            "changes_action_policy": False,
            "changes_portfolio": False,
            "submits_broker_order": False,
        },
        "note": "Decision recorded. No action policy, portfolio, or broker state was changed.",
    }


def summarize(df: pd.DataFrame, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        **meta,
        "row_count": int(len(df)),
        "sample": [_json_ready(row) for row in df.to_dict(orient="records")],
        "notes": [
            "Regime overlays are review-only proposals.",
            "Action policy must not consume proposed overlays until a reviewed promotion path is implemented.",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build review-only dynamic regime overlay proposals from macro/news evidence.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Asof date in YYYY-MM-DD")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--llm", action="store_true", help="Use Codex structured proposal generation.")
    parser.add_argument("--no-llm", action="store_true", help="Force deterministic fallback proposal.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.llm and args.no_llm:
        raise ValueError("Use only one of --llm or --no-llm")
    use_llm = bool(args.llm or (LLM_ENABLED and not args.no_llm))
    asof_date = _normalize_asof(args.date) if args.date else None
    df, meta = build_regime_overlay(
        asof_date=asof_date,
        lookback_days=max(int(args.lookback_days), 1),
        use_llm=use_llm,
        model=str(args.model),
    )
    if not args.dry_run:
        persist_regime_overlay(df, rebuild=bool(args.rebuild), asof_date=asof_date or pd.to_datetime(df["asof_date"].iloc[0], utc=True))
    payload = summarize(df, {**meta, "dry_run": bool(args.dry_run)})
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
