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
from utils.company_master import map_company_master_ids
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


env = Env()
env.read_env()

TABLE_NAME = "advisory_event_policy_actions"
EVENT_POLICY_SCHEMA_MIGRATION_ID = "20260611_advisory_event_policy_actions_base"
EVENT_POLICY_ACTIONABILITY_SCHEMA_MIGRATION_ID = "20260611_advisory_event_policy_actionability"
EVALUATIONS_TABLE = "advisory_event_evaluations"
REVIEWS_TABLE = "advisory_event_reviews"
DHAN_DAILY_TABLE = "dhan_ohlcv_daily"
PORTFOLIO_TABLE = "advisory_portfolio_orders"
PROMPT_ID = "event_policy_manual_review"
PROMPT_VERSION = registry_prompt_version(PROMPT_ID)
PROMPT_SCHEMA_VERSION = response_schema_version(PROMPT_ID)
EVENT_POLICY_LLM_MANUAL_REVIEW_ENABLED = env.bool("EVENT_POLICY_LLM_MANUAL_REVIEW_ENABLED", default=True)
EVENT_POLICY_LLM_MANUAL_REVIEW_MODEL = env("EVENT_POLICY_LLM_MANUAL_REVIEW_MODEL", default="codex")
EVENT_POLICY_LLM_MANUAL_REVIEW_MAX_ROWS = env.int("EVENT_POLICY_LLM_MANUAL_REVIEW_MAX_ROWS", default=25)
EVENT_POLICY_LLM_MANUAL_REVIEW_TIMEOUT_SECONDS = env.int("EVENT_POLICY_LLM_MANUAL_REVIEW_TIMEOUT_SECONDS", default=180)
EVENT_POLICY_LLM_AUTHORITY_CONTRACT = {
    "authority_scope": "event_policy_review_input_only",
    "action_policy_effect": "classify_ambiguous_event_policy_rows_only",
    "allowed_final_action_types": ["MANUAL_REVIEW", "NO_ACTION", "BUY_WATCH", "REDUCE_EXPOSURE_REVIEW"],
    "portfolio_authority": "none",
    "broker_execution_allowed": False,
    "policy_auto_promotion_allowed": False,
    "requires_downstream_policy_gate": True,
    "requires_technical_confirmation_for_entry": True,
    "requires_lifecycle_confirmation_for_exit_or_derisk": True,
    "resolved_review_only_actions": ["NO_ACTION", "BUY_WATCH", "REDUCE_EXPOSURE_REVIEW"],
}

POSITIVE_CLASSES = {
    "ORDER_WIN",
    "RESULTS_POSITIVE",
    "GROWTH_ACCELERATION",
    "MARGIN_EXPANSION",
    "GUIDANCE_UPGRADE",
    "PLEDGE_DOWN",
    "PROMOTER_BUYING",
    "BUYBACK",
    "CAPEX_EXPANSION",
    "POLICY_SECTOR_POSITIVE",
}
NEGATIVE_CLASSES = {
    "RESULTS_NEGATIVE",
    "GUIDANCE_DOWNGRADE",
    "PLEDGE_UP",
    "PROMOTER_SELLING",
    "MANAGEMENT_RESIGNATION",
    "REGULATORY_NOTICE",
    "AUDITOR_GOVERNANCE",
    "POLICY_SECTOR_NEGATIVE",
}
REVIEW_CLASSES = {"DILUTION", "RESULTS_MIXED", "CAPEX_EXPANSION"}
LOW_ACTION_CLASSES = {"DIVIDEND", "ANALYST_MEET", "CORPORATE_ACTION_NEUTRAL", "OTHER"}

EVENT_CLASS_LABELS = {
    "REGULATORY_NOTICE": "regulatory or tax notice",
    "POLICY_SECTOR_NEGATIVE": "sector policy risk",
    "POLICY_SECTOR_POSITIVE": "sector policy support",
    "ANALYST_MEET": "analyst or investor meeting",
    "ORDER_WIN": "order win",
    "RESULTS_POSITIVE": "positive results",
    "RESULTS_NEGATIVE": "weak results",
    "RESULTS_MIXED": "mixed results",
    "GROWTH_ACCELERATION": "growth acceleration",
    "MARGIN_EXPANSION": "margin expansion",
    "GUIDANCE_UPGRADE": "guidance upgrade",
    "GUIDANCE_DOWNGRADE": "guidance downgrade",
    "PLEDGE_UP": "promoter pledge increase",
    "PLEDGE_DOWN": "promoter pledge reduction",
    "PROMOTER_BUYING": "promoter buying",
    "PROMOTER_SELLING": "promoter selling",
    "MANAGEMENT_RESIGNATION": "senior management resignation",
    "AUDITOR_GOVERNANCE": "auditor or governance concern",
    "DILUTION": "dilution or fund raise",
    "BUYBACK": "buyback",
    "DIVIDEND": "dividend",
    "CORPORATE_ACTION_NEUTRAL": "routine corporate action",
    "OTHER": "company event",
}


def _event_label(policy_class: str) -> str:
    key = _text(policy_class).upper()
    return EVENT_CLASS_LABELS.get(key, key.replace("_", " ").lower() if key else "company event")
MATERIALITY_ORDER = {"low": 1, "medium": 2, "high": 3}
RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}
EVENT_POLICY_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        published_on TIMESTAMPTZ NOT NULL,
        asof_date TIMESTAMPTZ,
        policy_at TIMESTAMPTZ,
        setup_id TEXT NOT NULL,
        setup_name TEXT,
        symbol TEXT NOT NULL,
        company_master_id TEXT,
        unique_id TEXT NOT NULL,
        event_source TEXT,
        event_class TEXT,
        policy_class TEXT,
        source_verdict TEXT,
        source_setup_effect TEXT,
        review_action TEXT,
        action_type TEXT,
        action_status TEXT,
        policy_score DOUBLE PRECISION,
        confidence DOUBLE PRECISION,
        action_reason TEXT,
        action_detail TEXT,
        checks_json TEXT,
        operator_notes_json TEXT,
        llm_review_json TEXT,
        llm_prompt_id TEXT,
        llm_prompt_version TEXT,
        llm_prompt_schema_version TEXT,
        llm_review_status TEXT,
        llm_review_model TEXT,
        llm_review_error TEXT,
        raw_context_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (published_on, setup_id, symbol, unique_id)
    )
    """,
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS operator_notes_json TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS llm_review_json TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS llm_prompt_id TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS llm_prompt_version TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS llm_prompt_schema_version TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS llm_review_status TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS llm_review_model TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS llm_review_error TEXT",
]
EVENT_POLICY_ACTIONABILITY_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS actionability_json TEXT",
]


class EventPolicyManualReview(BaseModel):
    final_action_type: Literal["MANUAL_REVIEW", "NO_ACTION", "BUY_WATCH", "REDUCE_EXPOSURE_REVIEW"]
    confidence: float = Field(ge=0.0, le=1.0)
    operator_summary: str = Field(min_length=10, max_length=800)
    possible_action: str = Field(min_length=5, max_length=400)
    wait_for_events: list[str] = Field(default_factory=list, max_length=8)
    operator_questions: list[str] = Field(default_factory=list, max_length=8)
    downgrade_reason: str | None = Field(default=None, max_length=500)
    rationale: str = Field(min_length=10, max_length=1200)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.event_policy",
            fallback_type="event_policy_text_missing_check_failed",
            source="text",
            severity="warn",
            reason="Event-policy normalization could not evaluate missingness while normalizing text and kept string conversion fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "null", "<na>"}:
        return default
    return text


def _num(value: Any, default: float = 0.0) -> float:
    out = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(out) else float(out)


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "y"}:
        return True
    if text in {"0", "false", "f", "no", "n"}:
        return False
    return default


def _timestamp_text(value: Any) -> str:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return ""
    return ts.isoformat()


def _event_age_days(row: pd.Series) -> float | None:
    published = pd.to_datetime(row.get("published_on"), utc=True, errors="coerce")
    asof = pd.to_datetime(row.get("asof_date"), utc=True, errors="coerce")
    if pd.isna(published) or pd.isna(asof):
        return None
    return round(max(0.0, float((asof - published).total_seconds()) / 86400.0), 3)


def _freshness_bucket(age_days: float | None, expected_decay_days: float) -> str:
    if age_days is None:
        return "unknown"
    if age_days <= 2:
        return "fresh"
    if expected_decay_days > 0 and age_days <= expected_decay_days:
        return "within_expected_decay"
    if age_days <= 14:
        return "aging"
    return "stale"


def _price_reaction_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value >= 0.05:
        return "strong_positive"
    if value >= 0.02:
        return "positive"
    if value <= -0.05:
        return "strong_negative"
    if value <= -0.02:
        return "negative"
    return "muted"


def _current_exposure_bucket(row: pd.Series) -> str:
    for column in ["current_position_state", "portfolio_state", "holding_state", "position_state"]:
        text = _text(row.get(column)).lower()
        if text:
            if any(token in text for token in ["hold", "open", "invested", "long", "approved", "trimmed"]):
                return "open_position"
            if any(token in text for token in ["watch", "candidate", "deferred", "planned"]):
                return "watching"
            if any(token in text for token in ["exit", "closed", "none", "flat"]):
                return "no_open_position"
            return text
    exposure_value = max(
        _num(row.get("approved_capital"), 0.0),
        _num(row.get("approved_allocation_inr"), 0.0),
        _num(row.get("allocated_capital"), 0.0),
        _num(row.get("current_position_value"), 0.0),
        _num(row.get("position_value"), 0.0),
    )
    return "open_position" if exposure_value > 0 else "unknown"


def _jsonish(value: Any, *, source: str = "event_policy_json_context") -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None or pd.isna(value):
        return None
    text = _text(value)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.event_policy",
            fallback_type="event_policy_json_parse_failed",
            source=source,
            severity="warn",
            reason="Event policy could not parse stored JSON context and used an empty/default value.",
            error=exc,
            metadata={
                "value_length": len(text),
                "value_excerpt": text[:240],
            },
        )
        return None


def _jsonish_text(value: Any, *, source: str = "event_policy_json_text_context") -> str:
    parsed = _jsonish(value, source=source)
    if parsed is None:
        return _text(value).lower()
    if isinstance(parsed, dict):
        return " ".join(f"{key}={val}" for key, val in parsed.items()).lower()
    if isinstance(parsed, list):
        return " ".join(str(item) for item in parsed).lower()
    return str(parsed).lower()


def _compact_text_list(value: Any, *, max_items: int = 12, source: str = "event_policy_compact_text_list") -> list[str]:
    parsed = _jsonish(value, source=source)
    if isinstance(parsed, dict):
        for key in ["items", "values", "symbols", "sectors", "peers"]:
            if key in parsed:
                return _compact_text_list(parsed.get(key), max_items=max_items)
        values = parsed.values()
    elif isinstance(parsed, list):
        values = parsed
    else:
        text = _text(value)
        values = text.replace(";", ",").split(",") if text else []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _text(item).strip()
        if not text:
            continue
        key = text.upper()
        if key in seen:
            continue
        out.append(text[:80])
        seen.add(key)
        if len(out) >= max_items:
            break
    return out


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if _text(value):
            return value
    return None


def _affected_market_scope(row: pd.Series) -> dict[str, Any]:
    tensor = _jsonish(row.get("event_tensor_json"), source="event_tensor_json")
    tensor = tensor if isinstance(tensor, dict) else {}
    sectors = _compact_text_list(
        _first_nonempty(row.get("affected_sectors_json"), tensor.get("affected_sectors")),
        max_items=10,
        source="affected_sectors_json",
    )
    peers = _compact_text_list(
        _first_nonempty(row.get("affected_peers_json"), tensor.get("affected_peers")),
        max_items=12,
        source="affected_peers_json",
    )
    if peers and sectors:
        scope_type = "sector_and_peer_group"
    elif peers:
        scope_type = "peer_group"
    elif sectors:
        scope_type = "sector"
    else:
        scope_type = "single_company_or_unknown"
    validated_peers = _compact_text_list(
        _first_nonempty(
            row.get("validated_affected_peers_json"),
            row.get("resolved_affected_peers_json"),
            row.get("identity_validated_peers_json"),
            tensor.get("validated_affected_peers"),
            tensor.get("resolved_affected_peers"),
        ),
        max_items=12,
        source="validated_affected_peers_json",
    )
    unresolved_peers = [peer for peer in peers if peer.upper() not in {value.upper() for value in validated_peers}]
    identity_status = (
        "not_applicable"
        if not peers and not sectors
        else "validated"
        if peers and not unresolved_peers and validated_peers
        else "requires_identity_validation"
    )
    return {
        "scope_type": scope_type,
        "affected_sectors": sectors,
        "affected_peers": peers,
        "validated_affected_peers": validated_peers,
        "unresolved_affected_peers": unresolved_peers,
        "affected_sector_count": len(sectors),
        "affected_peer_count": len(peers),
        "identity_validation": {
            "status": identity_status,
            "validated_peer_count": len(validated_peers),
            "unresolved_peer_count": len(unresolved_peers),
            "requires_company_master_resolution": bool(unresolved_peers),
            "policy_effect": "context_only_until_resolved" if unresolved_peers else "identity_context_accepted",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "note": (
                "Affected peers came from structured extraction but are not marked as company-master/security-master validated."
                if unresolved_peers
                else "No unresolved affected peers are present."
            ),
        },
        "note": "Derived from structured event evaluation affected_sectors/affected_peers fields when available.",
    }


def annotate_policy_input_peer_identity(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty or "affected_peers_json" not in rows.columns:
        return rows
    out = rows.copy()
    row_peers: list[list[str]] = []
    all_peers: list[str] = []
    for value in out["affected_peers_json"].tolist():
        peers = _compact_text_list(value, max_items=12, source="affected_peers_json")
        row_peers.append(peers)
        all_peers.extend(peers)
    unique_peers = sorted({peer.upper() for peer in all_peers if _text(peer)})
    if not unique_peers:
        out["validated_affected_peers_json"] = "[]"
        out["unresolved_affected_peers_json"] = "[]"
        return out
    try:
        nse_ids = map_company_master_ids(unique_peers, exchange="NSE")
        bse_ids = map_company_master_ids(unique_peers, exchange="BSE")
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.event_policy",
            fallback_type="event_policy_affected_peer_identity_lookup_failed",
            source="company_master",
            severity="warn",
            reason="Event-policy input peer identity validation failed; affected peers remain context-only until resolved.",
            error=exc,
            metadata={"peer_count": len(unique_peers), "peer_sample": unique_peers[:10]},
        )
        out["validated_affected_peers_json"] = "[]"
        out["unresolved_affected_peers_json"] = out["affected_peers_json"].apply(
            lambda value: json_dumps(_compact_text_list(value, max_items=12, source="affected_peers_json"))
        )
        return out
    resolved: set[str] = set()
    for peer, nse_id, bse_id in zip(unique_peers, nse_ids.tolist(), bse_ids.tolist(), strict=False):
        if _text(nse_id) or _text(bse_id):
            resolved.add(peer.upper())
    validated_rows: list[str] = []
    unresolved_rows: list[str] = []
    for peers in row_peers:
        validated = [peer for peer in peers if peer.upper() in resolved]
        unresolved = [peer for peer in peers if peer.upper() not in resolved]
        validated_rows.append(json_dumps(validated))
        unresolved_rows.append(json_dumps(unresolved))
    out["validated_affected_peers_json"] = validated_rows
    out["unresolved_affected_peers_json"] = unresolved_rows
    return out


def _source_quality(row: pd.Series) -> dict[str, Any]:
    source = _text(row.get("event_source"), "unknown").lower()
    source_reliability = _text(row.get("source_reliability"), "medium").lower()
    parse_status = _text(row.get("parse_status"), "unknown").lower()
    trace_text = _jsonish_text(row.get("source_trace_json"), source="source_trace_json")
    tensor_text = _jsonish_text(row.get("event_tensor_json"), source="event_tensor_json")
    evidence_text = " ".join([source, parse_status, trace_text, tensor_text])

    if any(token in evidence_text for token in ["ocr", "document", "pdf", "raw_announcement_document_fallback"]):
        family = "derived_document"
        authority = "derived"
        base = "medium"
        confirmation_required = True
        rationale = "Document/OCR-derived evidence depends on parse quality and should be reviewed before action."
    elif any(token in source for token in ["exchange", "announcement", "nse"]) and not any(
        token in source for token in ["news", "rss"]
    ):
        family = "exchange_filing"
        authority = "primary"
        base = "high"
        confirmation_required = False
        rationale = "Exchange/company announcement source is primary evidence."
    elif any(token in evidence_text for token in ["news", "rss", "economic_times", "economictimes"]):
        family = "market_news"
        authority = "secondary"
        base = "medium"
        confirmation_required = True
        rationale = "News/RSS is useful context but should be confirmed by primary filings before action."
    else:
        family = "unknown"
        authority = "unknown"
        base = "medium" if source_reliability == "high" else source_reliability if source_reliability in {"low", "medium"} else "unknown"
        confirmation_required = True
        rationale = "Source type is not mapped; verify reliability manually."

    parse_good = parse_status in {"completed", "complete", "parsed", "success", "ok", "rss"}
    parse_bad = parse_status in {"failed", "error", "timeout", "ocr_failed", "parse_failed", "unparsed"}
    if parse_bad:
        base = "low"
        confirmation_required = True
        rationale = f"{rationale} parse_status={parse_status} lowers source quality."
    elif source_reliability == "low":
        base = "low"
        confirmation_required = True
        rationale = f"{rationale} Extractor marked source_reliability=low."
    elif source_reliability == "high" and base == "medium" and family == "derived_document" and parse_good:
        rationale = f"{rationale} Parsed document has high extractor reliability, but derived evidence remains capped at medium."
    elif source_reliability == "high" and base == "medium" and family == "unknown":
        rationale = f"{rationale} Extractor reliability is high, but source type is unknown so authority remains capped."
    elif source_reliability == "medium" and base == "high":
        rationale = f"{rationale} Extractor reliability is medium; keep primary-source authority but require normal review."

    confidence_ceiling = {"high": 0.9, "medium": 0.7, "low": 0.45}.get(base, 0.5)
    return {
        "source_type": source or "unknown",
        "source_family": family,
        "quality": base,
        "authority": authority,
        "source_reliability": source_reliability or "unknown",
        "parse_status": parse_status or "unknown",
        "confirmation_required": confirmation_required,
        "confidence_ceiling": confidence_ceiling,
        "rationale": rationale,
    }


def build_actionability_context(row: pd.Series, *, policy_class: str, action_type: str, action_status: str) -> dict[str, Any]:
    score_impact = _num(row.get("score_impact"))
    confidence = _num(row.get("confidence"))
    materiality = _text(row.get("materiality"), "low").lower()
    expected_decay_days = _num(row.get("expected_decay_days"), 0.0)
    age_days = _event_age_days(row)
    price_reaction = None
    for column in ["price_reaction", "price_reaction_pct", "event_return", "same_day_return", "return_1d"]:
        if _text(row.get(column)):
            price_reaction = _num(row.get(column))
            break
    market_scope = _affected_market_scope(row)
    next_evidence: list[str] = []
    if action_type in {"MANUAL_REVIEW", "REDUCE_EXPOSURE_REVIEW"}:
        next_evidence.extend(
            [
                "Latest price/volume reaction versus the event day",
                "Whether the stock is currently held, watched, or flat",
                "Primary-source follow-up or management clarification if available",
            ]
        )
    if policy_class in NEGATIVE_CLASSES:
        next_evidence.append("Current stop/invalidation level and whether it has been breached")
    if policy_class in POSITIVE_CLASSES:
        next_evidence.append("Technical trigger confirmation and liquidity check before any entry")
    if price_reaction is None:
        next_evidence.append("Point-in-time price reaction is missing; refresh OHLCV/event evidence before deciding")
    if market_scope["affected_sector_count"] or market_scope["affected_peer_count"]:
        next_evidence.append("Check whether affected peers/sectors confirm or contradict the event thesis")
    if market_scope.get("identity_validation", {}).get("requires_company_master_resolution"):
        next_evidence.append("Resolve extracted affected peers through company/security masters before using peer context")
    return {
        "version": 1,
        "materiality": materiality,
        "confidence": round(float(confidence), 4),
        "score_impact": round(float(score_impact), 4),
        "freshness": {
            "published_on": _timestamp_text(row.get("published_on")),
            "asof_date": _timestamp_text(row.get("asof_date")),
            "age_days": age_days,
            "expected_decay_days": expected_decay_days,
            "bucket": _freshness_bucket(age_days, expected_decay_days),
        },
        "current_exposure": {
            "bucket": _current_exposure_bucket(row),
            "note": "Derived from optional current portfolio/position columns when present; otherwise unknown.",
        },
        "price_reaction": {
            "value": price_reaction,
            "bucket": _price_reaction_bucket(price_reaction),
            "note": "Uses optional point-in-time event-return columns when available.",
        },
        "source_quality": _source_quality(row),
        "market_scope": market_scope,
        "suggested_next_evidence": list(dict.fromkeys(next_evidence))[:8],
        "review_priority": (
            "high"
            if action_type == "REDUCE_EXPOSURE_REVIEW" or materiality == "high" or abs(score_impact) >= 0.25
            else "medium"
            if action_type == "MANUAL_REVIEW" or materiality == "medium" or abs(score_impact) >= 0.12
            else "low"
        ),
        "deterministic_boundary": {
            "final_action_authority": "action_consolidation",
            "broker_executable": False,
            "policy_action_type": action_type,
            "policy_action_status": action_status,
        },
    }


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
    df = sql_to_df(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        """,
        params=(table_name,),
    )
    return set(df["column_name"].astype(str).tolist()) if not df.empty else set()


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=EVENT_POLICY_SCHEMA_MIGRATION_ID,
        description="Create event-policy action overlay table.",
        statements=EVENT_POLICY_SCHEMA_STATEMENTS,
        metadata={"tables": [TABLE_NAME], "authority_scope": "review_overlay_only"},
    )
    apply_schema_migration(
        migration_id=EVENT_POLICY_ACTIONABILITY_SCHEMA_MIGRATION_ID,
        description="Add event-policy actionability context for operator review.",
        statements=EVENT_POLICY_ACTIONABILITY_SCHEMA_STATEMENTS,
        metadata={"tables": [TABLE_NAME], "authority_scope": "review_overlay_only", "additive": True},
    )


def load_policy_inputs(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> pd.DataFrame:
    if not _table_exists(EVALUATIONS_TABLE):
        return pd.DataFrame()
    clauses = ["1 = 1"]
    params: dict[str, Any] = {}
    if asof_date is not None:
        clauses.append("e.asof_date = %(asof_date)s")
        params["asof_date"] = pd.to_datetime(asof_date, utc=True, errors="coerce").normalize()
    else:
        clauses.append(f"e.asof_date = (SELECT MAX(asof_date) FROM {EVALUATIONS_TABLE})")
    if symbols:
        clauses.append("UPPER(TRIM(e.symbol)) = ANY(%(symbols)s)")
        params["symbols"] = [str(value).strip().upper() for value in symbols]
    if setup_ids:
        clauses.append("UPPER(TRIM(e.setup_id)) = ANY(%(setup_ids)s)")
        params["setup_ids"] = [str(value).strip().upper() for value in setup_ids]

    review_select = ""
    review_join = ""
    if _table_exists(REVIEWS_TABLE):
        review_select = """
            , r.review_action
            , r.review_score
            , r.veto
            , r.review_reason
            , r.review_flags_json
        """
        review_join = f"""
        LEFT JOIN LATERAL (
            SELECT
                r.review_action,
                r.review_score,
                r.veto,
                r.review_reason,
                r.review_flags_json
            FROM {REVIEWS_TABLE} r
            WHERE r.published_on = e.published_on
              AND r.setup_id = e.setup_id
              AND r.symbol = e.symbol
              AND r.unique_id = e.unique_id
            ORDER BY r.reviewed_at DESC NULLS LAST
            LIMIT 1
        ) r ON TRUE
        """
    price_select = ""
    price_join = ""
    dhan_columns = _table_columns(DHAN_DAILY_TABLE) if _table_exists(DHAN_DAILY_TABLE) else set()
    if {"ticker", "date", "close"}.issubset(dhan_columns):
        daily_desc_order = "d.date DESC NULLS LAST" + (", d.load_ts DESC NULLS LAST" if "load_ts" in dhan_columns else "")
        daily_asc_order = "d.date ASC NULLS LAST" + (", d.load_ts DESC NULLS LAST" if "load_ts" in dhan_columns else "")
        price_select = """
            , current_px.close AS current_reference_price
            , current_px.date AS current_reference_date
            , event_px.close AS event_reference_price
            , event_px.date AS event_reference_date
            , CASE
                WHEN current_px.close IS NOT NULL AND event_px.close IS NOT NULL AND event_px.close > 0
                THEN (current_px.close / event_px.close) - 1.0
                ELSE NULL
              END AS price_reaction_pct
        """
        price_join = f"""
        LEFT JOIN LATERAL (
            SELECT d.close, d.date
            FROM {DHAN_DAILY_TABLE} d
            WHERE UPPER(TRIM(d.ticker)) = UPPER(TRIM(e.symbol))
              AND d.close IS NOT NULL
              AND d.date::date <= e.asof_date::date
            ORDER BY {daily_desc_order}
            LIMIT 1
        ) current_px ON TRUE
        LEFT JOIN LATERAL (
            SELECT d.close, d.date
            FROM {DHAN_DAILY_TABLE} d
            WHERE UPPER(TRIM(d.ticker)) = UPPER(TRIM(e.symbol))
              AND d.close IS NOT NULL
              AND d.date::date >= e.published_on::date
              AND d.date::date <= e.asof_date::date
            ORDER BY {daily_asc_order}
            LIMIT 1
        ) event_px ON TRUE
        """
    portfolio_select = ""
    portfolio_join = ""
    portfolio_columns = _table_columns(PORTFOLIO_TABLE) if _table_exists(PORTFOLIO_TABLE) else set()
    if {"symbol", "asof_date", "portfolio_status", "approved_allocation_inr"}.issubset(portfolio_columns):
        portfolio_order_parts = ["p.asof_date DESC NULLS LAST"]
        if "published_on" in portfolio_columns:
            portfolio_order_parts.append("p.published_on DESC NULLS LAST")
        if "load_ts" in portfolio_columns:
            portfolio_order_parts.append("p.load_ts DESC NULLS LAST")
        portfolio_reason_expr = "p.portfolio_reason" if "portfolio_reason" in portfolio_columns else "NULL"
        portfolio_select = """
            , portfolio_ctx.portfolio_status AS current_position_state
            , portfolio_ctx.approved_allocation_inr AS approved_allocation_inr
            , portfolio_ctx.portfolio_reason AS current_position_reason
        """
        portfolio_join = f"""
        LEFT JOIN LATERAL (
            SELECT p.portfolio_status, p.approved_allocation_inr, {portfolio_reason_expr} AS portfolio_reason
            FROM {PORTFOLIO_TABLE} p
            WHERE UPPER(TRIM(p.symbol)) = UPPER(TRIM(e.symbol))
              AND p.asof_date::date <= e.asof_date::date
            ORDER BY {', '.join(portfolio_order_parts)}
            LIMIT 1
        ) portfolio_ctx ON TRUE
        """
    df = sql_to_df(
        f"""
        SELECT
            e.published_on,
            e.asof_date,
            e.setup_id,
            e.setup_name,
            e.symbol,
            e.company_master_id,
            e.unique_id,
            e.event_source,
            e.subject,
            e.parse_status,
            e.materiality,
            e.setup_effect,
            e.direction,
            e.surprise,
            e.novelty,
            e.contradiction,
            e.expected_decay_days,
            e.source_reliability,
            e.affected_sectors_json,
            e.affected_peers_json,
            e.governance_risk,
            e.balance_sheet_risk,
            e.execution_risk,
            e.investable_now,
            e.verdict,
            e.event_class,
            e.state_transition_hint,
            e.score_impact,
            e.confidence,
            e.what_happened,
            e.rationale,
            e.event_tensor_json,
            e.source_trace_json
            {review_select}
            {price_select}
            {portfolio_select}
        FROM {EVALUATIONS_TABLE} e
        {review_join}
        {price_join}
        {portfolio_join}
        WHERE {' AND '.join(clauses)}
        ORDER BY e.published_on DESC, e.setup_id, e.symbol
        """,
        params=params or None,
        retries=3,
    )
    return annotate_policy_input_peer_identity(df)


def policy_class_for_event(row: pd.Series) -> str:
    event_class = _text(row.get("event_class"), "OTHER").upper()
    text = " ".join(
        [
            _text(row.get("subject")),
            _text(row.get("what_happened")),
            _text(row.get("rationale")),
        ]
    ).lower()
    if event_class == "OTHER":
        if any(token in text for token in ["received orders", "export orders", "orders worth", "work order", "letter of award", "contract award"]):
            return "ORDER_WIN"
        if any(token in text for token in ["show cause", "regulatory notice", "sebi notice", "rbi notice", "investigation", "raid"]):
            return "REGULATORY_NOTICE"
        if "resignation" in text and any(token in text for token in ["auditor", "cfo", "ceo", "managing director", "independent director", "compliance officer"]):
            return "MANAGEMENT_RESIGNATION"
        if any(token in text for token in ["revenue was", "sales were", "ebitda", "pat", "profit"]) and _text(row.get("setup_effect")) == "strengthens":
            return "GROWTH_ACCELERATION"
    return event_class


def _has_high_risk(row: pd.Series) -> bool:
    return any(
        RISK_ORDER.get(_text(row.get(column), "none").lower(), 0) >= RISK_ORDER["high"]
        for column in ["governance_risk", "balance_sheet_risk", "execution_risk"]
    )


def build_policy_for_event(row: pd.Series) -> dict[str, Any]:
    policy_class = policy_class_for_event(row)
    verdict = _text(row.get("verdict"), "review_manual").lower()
    setup_effect = _text(row.get("setup_effect"), "neutral").lower()
    materiality = _text(row.get("materiality"), "low").lower()
    review_action = _text(row.get("review_action")).lower()
    score_impact = _num(row.get("score_impact"))
    confidence = _num(row.get("confidence"))
    is_veto = _bool(row.get("veto"))
    mat_score = MATERIALITY_ORDER.get(materiality, 1)
    checks: list[dict[str, Any]] = []

    action_type = "NO_ACTION"
    action_status = "context_only"
    reason = "Event is informational or too weak for action."
    policy_score = score_impact

    if is_veto or review_action == "veto":
        action_type = "REDUCE_EXPOSURE_REVIEW" if policy_class in NEGATIVE_CLASSES else "MANUAL_REVIEW"
        action_status = "blocked_by_adversarial_review"
        reason = "Adversarial review vetoed or blocked the event; operator should review exposure before acting."
        policy_score = min(policy_score, -0.5)
        checks.append({"check_type": "adversarial_review", "blocking": True, "rationale": "Reviewer veto or equivalent block is present."})
    elif policy_class in NEGATIVE_CLASSES:
        action_type = "REDUCE_EXPOSURE_REVIEW" if mat_score >= 2 or score_impact <= -0.12 else "MANUAL_REVIEW"
        action_status = "risk_overlay"
        reason = f"This {_event_label(policy_class)} may weaken the thesis. Review materiality, current exposure, stop level, and whether the event is fresh before changing the position."
        policy_score = min(policy_score, -0.2)
        checks.append({"check_type": "negative_event_class", "blocking": action_type == "REDUCE_EXPOSURE_REVIEW", "rationale": _event_label(policy_class)})
    elif policy_class == "DILUTION":
        action_type = "MANUAL_REVIEW"
        action_status = "capital_structure_review"
        reason = "Dilution/fund-raise events require manual review of use of proceeds, pricing, and promoter participation."
        policy_score = min(policy_score, -0.05)
        checks.append({"check_type": "dilution_terms", "blocking": True, "rationale": "Check dilution size and use of funds."})
    elif policy_class in POSITIVE_CLASSES:
        if verdict == "continue" and setup_effect == "strengthens" and mat_score >= 2 and confidence >= 0.55 and score_impact >= 0.12 and not _has_high_risk(row):
            action_type = "BUY_WATCH"
            action_status = "positive_watch_overlay"
            reason = f"This {_event_label(policy_class)} looks material and supportive. Treat it as watchlist evidence until technical and risk checks confirm an entry."
            checks.append({"check_type": "technical_confirmation", "blocking": True, "rationale": "Do not buy solely on the event; require technical trigger and liquidity checks."})
        else:
            action_type = "MANUAL_REVIEW"
            action_status = "positive_but_incomplete"
            reason = f"This {_event_label(policy_class)} may be positive, but the evidence is not clean enough yet. Check materiality, confidence, risks, and whether the move is already priced in."
            checks.append({"check_type": "evidence_quality", "blocking": True, "rationale": "Check materiality, confidence, risks, and whether the event is already priced in."})
    elif policy_class in REVIEW_CLASSES or verdict == "review_manual":
        action_type = "MANUAL_REVIEW"
        action_status = "manual_review_required"
        reason = f"This {_event_label(policy_class)} needs human interpretation before it can affect the recommendation."
        checks.append({"check_type": "manual_interpretation", "blocking": True, "rationale": "Event class is mixed or context-sensitive."})

    if action_type == "MANUAL_REVIEW" and policy_class in LOW_ACTION_CLASSES and mat_score <= 1 and confidence < 0.55 and abs(score_impact) < 0.12:
        action_type = "NO_ACTION"
        action_status = "low_information_downgraded"
        reason = "Event is low-materiality, low-confidence, and has no actionable policy edge; ignore until stronger follow-up evidence appears."
        checks.append({"check_type": "low_information_filter", "blocking": False, "rationale": "No operator review needed unless a stronger related event appears."})

    if review_action in {"penalize", "review_manual"} and action_type == "BUY_WATCH":
        action_type = "MANUAL_REVIEW"
        action_status = "downgraded_by_adversarial_review"
        reason = "Positive event was downgraded because adversarial review requires manual verification."
        checks.append({"check_type": "adversarial_review", "blocking": True, "rationale": f"review_action={review_action}"})

    actionability = build_actionability_context(row, policy_class=policy_class, action_type=action_type, action_status=action_status)
    raw_context = {
        "event_policy_version": 1,
        "event_class": _text(row.get("event_class")),
        "policy_class": policy_class,
        "verdict": verdict,
        "setup_effect": setup_effect,
        "materiality": materiality,
        "review_action": review_action,
        "veto": is_veto,
        "score_impact": score_impact,
        "confidence": confidence,
        "what_happened": _text(row.get("what_happened")),
        "rationale": _text(row.get("rationale")),
        "parse_status": _text(row.get("parse_status")),
        "affected_sectors": _compact_text_list(row.get("affected_sectors_json"), source="affected_sectors_json"),
        "affected_peers": _compact_text_list(row.get("affected_peers_json"), source="affected_peers_json"),
        "event_tensor": row.get("event_tensor_json"),
        "source_trace": _jsonish(row.get("source_trace_json"), source="source_trace_json") or row.get("source_trace_json"),
        "actionability": actionability,
    }
    return {
        "published_on": row.get("published_on"),
        "asof_date": row.get("asof_date"),
        "policy_at": pd.Timestamp.utcnow(),
        "setup_id": _text(row.get("setup_id")),
        "setup_name": _text(row.get("setup_name")),
        "symbol": _text(row.get("symbol")).upper(),
        "company_master_id": _text(row.get("company_master_id")),
        "unique_id": _text(row.get("unique_id")),
        "event_source": _text(row.get("event_source")),
        "event_class": _text(row.get("event_class"), "OTHER").upper(),
        "policy_class": policy_class,
        "source_verdict": verdict,
        "source_setup_effect": setup_effect,
        "review_action": review_action,
        "action_type": action_type,
        "action_status": action_status,
        "policy_score": round(float(policy_score), 4),
        "confidence": round(float(confidence), 4),
        "action_reason": reason,
        "action_detail": _text(row.get("what_happened"))[:1200],
        "checks_json": json_dumps(checks),
        "operator_notes_json": json_dumps({}),
        "llm_review_json": json_dumps({}),
        "llm_prompt_id": None,
        "llm_prompt_version": None,
        "llm_prompt_schema_version": None,
        "llm_review_status": "not_requested",
        "llm_review_model": None,
        "llm_review_error": None,
        "actionability_json": json_dumps(actionability),
        "raw_context_json": json_dumps(raw_context),
        "load_ts": pd.Timestamp.utcnow(),
    }


def _manual_review_prompt(policy_row: dict[str, Any]) -> str:
    payload = {
        "instruction": (
            "Review this event-policy MANUAL_REVIEW row. "
            "If manual review is unlikely to lead to a useful action, set final_action_type to NO_ACTION. "
            "If it is useful positive watchlist pressure but still needs technical/risk confirmation, set final_action_type to BUY_WATCH. "
            "If it is useful negative/de-risk pressure but still needs portfolio/lifecycle confirmation, set final_action_type to REDUCE_EXPOSURE_REVIEW. "
            "Keep MANUAL_REVIEW only when the event needs unresolved interpretation before even watch/de-risk classification. "
            "Always provide exact operator notes, future events to wait for, and questions to answer. "
            "Do not recommend immediate broker execution, sizing, or portfolio mutation."
        ),
        "authority_contract": EVENT_POLICY_LLM_AUTHORITY_CONTRACT,
        "policy_row": policy_row,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _deterministic_operator_notes(policy_row: dict[str, Any], *, status: str, error: str | None = None) -> dict[str, Any]:
    action_type = _text(policy_row.get("action_type"), "MANUAL_REVIEW").upper()
    policy_class = _text(policy_row.get("policy_class"), "OTHER")
    actionability = _jsonish(policy_row.get("actionability_json"), source="event_policy_manual_review_actionability_json")
    actionability = actionability if isinstance(actionability, dict) else {}
    raw_context = _jsonish(policy_row.get("raw_context_json"), source="event_policy_manual_review_raw_context_json")
    raw_context = raw_context if isinstance(raw_context, dict) else {}
    next_evidence = actionability.get("suggested_next_evidence") if isinstance(actionability, dict) else []
    materiality = _text((actionability or {}).get("materiality") if isinstance(actionability, dict) else None, _text(policy_row.get("materiality"), "low")).lower()
    confidence = _num(policy_row.get("confidence"))
    score_impact = _num(raw_context.get("score_impact"), _num(policy_row.get("score_impact"), _num(policy_row.get("policy_score"))))
    notes = {
        "final_action_type": action_type,
        "confidence": confidence,
        "operator_summary": _text(policy_row.get("action_reason"), "Manual review required before this event can affect an action."),
        "possible_action": "Keep this as review-only evidence until follow-up data confirms materiality and market reaction.",
        "wait_for_events": list(next_evidence)[:5]
        if isinstance(next_evidence, list) and next_evidence
        else [
            "Next price/volume reaction after the event",
            "Follow-up exchange clarification or management commentary",
            "Technical trigger or support failure",
        ],
        "operator_questions": [
            "Is the event material relative to revenue, market cap, or existing thesis?",
            "Is the event already priced in?",
            "Does technical structure confirm or contradict the event?",
        ],
        "actionability": actionability,
        "authority_contract": EVENT_POLICY_LLM_AUTHORITY_CONTRACT,
        "downgrade_reason": None,
        "rationale": "Deterministic fallback notes generated because LLM review was unavailable or disabled.",
        "status": status,
    }
    if policy_class in LOW_ACTION_CLASSES:
        notes["final_action_type"] = "NO_ACTION"
        notes["possible_action"] = "No action unless a stronger related event appears."
        notes["downgrade_reason"] = "Low-action event class is not worth manual review by itself."
    elif policy_class in POSITIVE_CLASSES:
        if materiality == "low" or confidence < 0.45 or score_impact < 0.05:
            notes["final_action_type"] = "NO_ACTION"
            notes["possible_action"] = "Ignore until the positive event has stronger materiality, confidence, or price/volume confirmation."
            notes["downgrade_reason"] = "Weak positive event evidence should not consume Manual Review or block technical candidates."
        else:
            notes["final_action_type"] = "BUY_WATCH"
            notes["possible_action"] = "Treat as positive watchlist pressure only; require technical trigger, liquidity, stop, target, and risk checks before any entry."
            notes["downgrade_reason"] = None
            notes["operator_summary"] = (
                "Positive event evidence is useful as watchlist pressure, but it is not strong enough to create broker-capable action."
            )
    elif policy_class in NEGATIVE_CLASSES:
        if materiality == "low" and confidence < 0.45 and abs(score_impact) < 0.05:
            notes["final_action_type"] = "NO_ACTION"
            notes["possible_action"] = "Ignore until the negative event has stronger materiality, confidence, or price/volume confirmation."
            notes["downgrade_reason"] = "Weak negative event evidence should not consume Manual Review or create de-risk pressure."
        else:
            notes["final_action_type"] = "REDUCE_EXPOSURE_REVIEW"
            notes["possible_action"] = "Treat as review-only de-risk pressure; require portfolio exposure, lifecycle, stop, and event-freshness confirmation before any exit or reduction."
            notes["downgrade_reason"] = None
            notes["operator_summary"] = (
                "Negative event evidence is useful as de-risk pressure, but it is not strong enough to create broker-capable exit."
            )
    if error:
        notes["llm_error"] = error
    return notes


def _apply_manual_review_notes(
    policy_row: dict[str, Any],
    *,
    notes: dict[str, Any],
    status: str,
    effective_model: str,
    error: str | None = None,
) -> dict[str, Any]:
    out = dict(policy_row)
    final_action = str(notes.get("final_action_type") or out.get("action_type") or "MANUAL_REVIEW").upper()
    notes["authority_contract"] = EVENT_POLICY_LLM_AUTHORITY_CONTRACT
    if final_action in {"MANUAL_REVIEW", "NO_ACTION", "BUY_WATCH", "REDUCE_EXPOSURE_REVIEW"}:
        out["action_type"] = final_action
    if final_action == "NO_ACTION":
        out["action_status"] = "llm_downgraded_no_action" if status == "ok" else "deterministic_downgraded_no_action"
        downgrade_reason = _text(notes.get("downgrade_reason"))
        if downgrade_reason:
            out["action_reason"] = downgrade_reason
    elif final_action == "BUY_WATCH":
        out["action_status"] = "llm_reclassified_watch_overlay" if status == "ok" else "deterministic_reclassified_watch_overlay"
        out["action_reason"] = _text(notes.get("operator_summary"), out.get("action_reason"))
    elif final_action == "REDUCE_EXPOSURE_REVIEW":
        out["action_status"] = "llm_reclassified_derisk_overlay" if status == "ok" else "deterministic_reclassified_derisk_overlay"
        out["action_reason"] = _text(notes.get("operator_summary"), out.get("action_reason"))
    elif final_action != "MANUAL_REVIEW":
        out["action_status"] = "llm_reclassified_review" if status == "ok" else "deterministic_reclassified_review"
        out["action_reason"] = _text(notes.get("operator_summary"), out.get("action_reason"))
    else:
        out["action_reason"] = _text(notes.get("operator_summary"), out.get("action_reason"))
    out["operator_notes_json"] = json_dumps(notes)
    out["llm_review_json"] = json_dumps(notes)
    out["llm_prompt_id"] = PROMPT_ID
    out["llm_prompt_version"] = PROMPT_VERSION
    out["llm_prompt_schema_version"] = PROMPT_SCHEMA_VERSION
    out["llm_review_status"] = status
    out["llm_review_model"] = effective_model
    out["llm_review_error"] = error
    raw_context = _jsonish(out.get("raw_context_json"), source="event_policy_manual_review_output_raw_context_json")
    raw_context = raw_context if isinstance(raw_context, dict) else {}
    raw_context["operator_notes"] = notes
    raw_context["authority_contract"] = EVENT_POLICY_LLM_AUTHORITY_CONTRACT
    raw_context["prompt_contract"] = {
        "prompt_id": PROMPT_ID,
        "prompt_version": PROMPT_VERSION,
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
    }
    out["raw_context_json"] = json_dumps(raw_context)
    return out


def repair_llm_provenance_metadata(*, dry_run: bool = True) -> dict[str, Any]:
    """Backfill prompt metadata for legacy LLM-relevant event-policy rows.

    This intentionally updates metadata only. It does not change actions,
    scores, review notes, portfolio state, or broker eligibility.
    """
    ensure_tables()
    where_clause = """
        COALESCE(LOWER(TRIM(llm_review_status)), '') NOT IN ('', 'not_requested')
        AND (
            llm_prompt_id IS NULL OR TRIM(llm_prompt_id) = ''
            OR llm_prompt_version IS NULL OR TRIM(llm_prompt_version) = ''
            OR llm_prompt_schema_version IS NULL OR TRIM(llm_prompt_schema_version) = ''
            OR llm_review_model IS NULL OR TRIM(llm_review_model) = ''
        )
    """
    count_df = sql_to_df(
        f"""
        SELECT
            COUNT(*) AS matched_rows,
            COUNT(*) FILTER (WHERE llm_prompt_id IS NULL OR TRIM(llm_prompt_id) = '') AS missing_prompt_id,
            COUNT(*) FILTER (WHERE llm_prompt_version IS NULL OR TRIM(llm_prompt_version) = '') AS missing_prompt_version,
            COUNT(*) FILTER (WHERE llm_prompt_schema_version IS NULL OR TRIM(llm_prompt_schema_version) = '') AS missing_prompt_schema_version,
            COUNT(*) FILTER (WHERE llm_review_model IS NULL OR TRIM(llm_review_model) = '') AS missing_model
        FROM {TABLE_NAME}
        WHERE {where_clause}
        """
    )
    summary = count_df.iloc[0].to_dict() if not count_df.empty else {}
    matched_rows = int(summary.get("matched_rows") or 0)
    payload = {
        "status": "dry_run" if dry_run else "applied",
        "table": TABLE_NAME,
        "dry_run": bool(dry_run),
        "matched_rows": matched_rows,
        "missing_prompt_id": int(summary.get("missing_prompt_id") or 0),
        "missing_prompt_version": int(summary.get("missing_prompt_version") or 0),
        "missing_prompt_schema_version": int(summary.get("missing_prompt_schema_version") or 0),
        "missing_model": int(summary.get("missing_model") or 0),
        "prompt_id": PROMPT_ID,
        "prompt_version": PROMPT_VERSION,
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "model_default": EVENT_POLICY_LLM_MANUAL_REVIEW_MODEL,
        "policy_boundary": {
            "metadata_only": True,
            "action_policy_changed": False,
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        },
    }
    if dry_run or matched_rows == 0:
        return payload

    def _apply_repair() -> int:
        with db_session() as (_, cur):
            cur.execute(
                f"""
                UPDATE {TABLE_NAME}
                SET
                    llm_prompt_id = COALESCE(NULLIF(TRIM(llm_prompt_id), ''), %s),
                    llm_prompt_version = COALESCE(NULLIF(TRIM(llm_prompt_version), ''), %s),
                    llm_prompt_schema_version = COALESCE(NULLIF(TRIM(llm_prompt_schema_version), ''), %s),
                    llm_review_model = COALESCE(NULLIF(TRIM(llm_review_model), ''), %s)
                WHERE {where_clause}
                """,
                (PROMPT_ID, PROMPT_VERSION, PROMPT_SCHEMA_VERSION, EVENT_POLICY_LLM_MANUAL_REVIEW_MODEL),
            )
            return int(cur.rowcount or 0)

    updated_rows = execute_db_operation(
        _apply_repair,
        operation_name="event_policy:repair_llm_provenance_metadata",
    )
    payload["updated_rows"] = int(updated_rows)
    return payload


def apply_llm_manual_review(policy_row: dict[str, Any], *, model: str | None = None, use_llm: bool = True) -> dict[str, Any]:
    if str(policy_row.get("action_type") or "").upper() != "MANUAL_REVIEW":
        return policy_row
    effective_model = model or EVENT_POLICY_LLM_MANUAL_REVIEW_MODEL
    if not use_llm or str(effective_model).strip().lower() in {"", "off", "none", "disabled", "false"}:
        notes = _deterministic_operator_notes(policy_row, status="disabled")
        status = "disabled"
        error = None
    else:
        try:
            codex_model = effective_model.split(":", 1)[1] if effective_model.startswith("codex:") else None if effective_model == "codex" else effective_model
            review = run_codex_structured(
                _manual_review_prompt(policy_row),
                response_model=EventPolicyManualReview,
                model=codex_model,
                system_prompt=(
                    "You are a cautious Indian-equity event-policy reviewer. "
                    "You decide whether a manual-review event should become no-action, review-only watch pressure, "
                    "review-only de-risk pressure, or remain manual review. "
                    "You never submit trades, size positions, mutate portfolios, or create broker-executable recommendations."
                ),
                max_attempts=2,
                timeout_seconds=EVENT_POLICY_LLM_MANUAL_REVIEW_TIMEOUT_SECONDS,
            )
            notes = review.model_dump()
            status = "ok"
            error = None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            notes = _deterministic_operator_notes(policy_row, status="fallback_after_error", error=error)
            status = "fallback_after_error"
            record_fallback_event(
                module="advisory.event_policy",
                source="event_policy_manual_review",
                fallback_type="llm_deterministic_fallback",
                severity="warn",
                symbol=policy_row.get("symbol"),
                unique_id=policy_row.get("unique_id"),
                reason="Event-policy manual-review LLM failed; deterministic operator notes were used.",
                deterministic_fallback=True,
                error=exc,
                metadata={"model": effective_model, "event_class": policy_row.get("policy_class") or policy_row.get("event_class")},
            )

    return _apply_manual_review_notes(policy_row, notes=notes, status=status, effective_model=effective_model, error=error)


def _manual_review_priority_key(policy_row: dict[str, Any]) -> tuple[int, int, float, float]:
    actionability = _jsonish(policy_row.get("actionability_json"), source="event_policy_actionability_json")
    actionability = actionability if isinstance(actionability, dict) else {}
    review_priority = str(actionability.get("review_priority") or "").strip().lower()
    priority_rank = {"high": 0, "medium": 1, "low": 2}.get(review_priority, 3)
    materiality = str(actionability.get("materiality") or policy_row.get("source_verdict") or "").strip().lower()
    materiality_rank = {"high": 0, "medium": 1, "low": 2}.get(materiality, 3)
    score = abs(_num(policy_row.get("policy_score"), 0.0))
    confidence = _num(policy_row.get("confidence"), 0.0)
    return (priority_rank, materiality_rank, -score, 1.0 - confidence)


def build_event_policy_actions(
    events: pd.DataFrame,
    *,
    use_llm: bool | None = None,
    llm_max_rows: int | None = None,
    model: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if events.empty:
        return pd.DataFrame(), {"input_rows": 0, "policy_rows": 0}
    policy_rows = [build_policy_for_event(row) for _, row in events.iterrows()]
    effective_use_llm = EVENT_POLICY_LLM_MANUAL_REVIEW_ENABLED if use_llm is None else bool(use_llm)
    max_rows = EVENT_POLICY_LLM_MANUAL_REVIEW_MAX_ROWS if llm_max_rows is None else max(0, int(llm_max_rows))
    reviewed_count = 0
    if effective_use_llm and max_rows > 0:
        manual_indices = [
            idx
            for idx, row in enumerate(policy_rows)
            if str(row.get("action_type") or "").upper() == "MANUAL_REVIEW"
        ]
        manual_indices.sort(key=lambda idx: _manual_review_priority_key(policy_rows[idx]))
        for idx in manual_indices:
            row = policy_rows[idx]
            if str(row.get("action_type") or "").upper() != "MANUAL_REVIEW":
                continue
            if reviewed_count >= max_rows:
                policy_rows[idx] = _apply_manual_review_notes(
                    row,
                    notes=_deterministic_operator_notes(row, status="skipped_limit"),
                    status="skipped_limit",
                    effective_model=model or EVENT_POLICY_LLM_MANUAL_REVIEW_MODEL,
                    error=None,
                )
                continue
            policy_rows[idx] = apply_llm_manual_review(row, model=model, use_llm=True)
            reviewed_count += 1
    elif not effective_use_llm:
        for idx, row in enumerate(policy_rows):
            if str(row.get("action_type") or "").upper() == "MANUAL_REVIEW":
                policy_rows[idx] = apply_llm_manual_review(row, model=model, use_llm=False)

    out = pd.DataFrame(policy_rows)
    for column in ["published_on", "asof_date", "policy_at", "load_ts"]:
        out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    return out, {
        "input_rows": int(len(events)),
        "policy_rows": int(len(out)),
        "action_counts": out["action_type"].value_counts(dropna=False).to_dict(),
        "policy_class_counts": out["policy_class"].value_counts(dropna=False).head(20).to_dict(),
        "llm_manual_review_enabled": bool(effective_use_llm),
        "llm_manual_review_rows": int(reviewed_count),
    }


def persist_event_policy_actions(df: pd.DataFrame) -> None:
    ensure_tables()
    if df.empty:
        return
    upsert_to_db(
        df,
        TABLE_NAME,
        unique_keys=["published_on", "setup_id", "symbol", "unique_id"],
        timescaledb_column="published_on",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply deterministic event-class policies to LLM event evaluations.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--setup", dest="setup_ids", nargs="*")
    parser.add_argument("--model", default=None)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--llm-max-rows", type=int, default=None)
    parser.add_argument("--repair-llm-provenance", action="store_true", help="Backfill prompt metadata on legacy LLM-relevant event-policy rows.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.repair_llm_provenance:
        payload = repair_llm_provenance_metadata(dry_run=bool(args.dry_run))
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return 0
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    events = load_policy_inputs(asof_date=asof_date, symbols=args.symbols, setup_ids=args.setup_ids)
    actions, meta = build_event_policy_actions(
        events,
        use_llm=not bool(args.no_llm),
        llm_max_rows=args.llm_max_rows,
        model=args.model,
    )
    if not args.dry_run:
        persist_event_policy_actions(actions)
    print(json.dumps({"status": "ok", "table": TABLE_NAME, **meta, "dry_run": bool(args.dry_run)}, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
