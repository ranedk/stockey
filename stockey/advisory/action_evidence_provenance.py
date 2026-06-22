from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any

import pandas as pd

from advisory.action_recommender import TABLE_NAME as ACTION_RECOMMENDATIONS_TABLE
from advisory.event_evidence_store import table_exists
from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


TABLE_NAME = "advisory_action_evidence_provenance"
SCHEMA_MIGRATION_ID = "20260621_advisory_action_evidence_provenance_base"

CONTEXT_OVERLAY_SOURCE_TABLES = {
    "announcement_context": "advisory_announcement_context_overlays",
    "bhavcopy_context": "advisory_bhavcopy_context_overlays",
    "exchange_context": "advisory_exchange_context_overlays",
    "macro_context": "advisory_macro_context_overlays",
    "theme_context": "advisory_news_theme_context_overlays",
}
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_LIMIT = 10000

SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        provenance_id TEXT PRIMARY KEY,
        asof_date TIMESTAMPTZ NOT NULL,
        published_on TIMESTAMPTZ,
        symbol TEXT NOT NULL,
        setup_id TEXT,
        unique_id TEXT,
        action_code TEXT NOT NULL,
        action_source TEXT,
        evidence_section TEXT NOT NULL,
        evidence_node_type TEXT NOT NULL,
        evidence_status TEXT NOT NULL,
        influence_direction TEXT,
        influence_effect TEXT NOT NULL,
        source_ref_json TEXT,
        evidence_node_json TEXT,
        action_node_json TEXT,
        authority_scope TEXT NOT NULL,
        policy_effect TEXT NOT NULL,
        broker_execution_allowed BOOLEAN NOT NULL DEFAULT FALSE,
        policy_auto_promotion_allowed BOOLEAN NOT NULL DEFAULT FALSE,
        load_ts TIMESTAMPTZ NOT NULL
    )
    """,
]


def _record_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.action_evidence_provenance",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _json_clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_clean(item) for item in value]
    if isinstance(value, tuple):
        return [_json_clean(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if value is pd.NaT:
        return None
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return None
    return value


def _parse_jsonish(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return default
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return default
    try:
        parsed = json.loads(str(value))
    except Exception as exc:
        _record_fallback(
            fallback_type="action_evidence_provenance_json_parse_failed",
            source=ACTION_RECOMMENDATIONS_TABLE,
            reason="Action evidence provenance could not parse JSON payload.",
            error=exc,
            metadata={"payload_length": len(str(value))},
        )
        return default
    return parsed


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _bool_or_false(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return False
    return str(value).strip().lower() in {"true", "t", "1", "yes", "y"}


def _provenance_id(row: dict[str, Any]) -> str:
    parts = [
        row.get("asof_date"),
        row.get("symbol"),
        row.get("setup_id"),
        row.get("unique_id"),
        row.get("action_code"),
        row.get("evidence_section"),
        row.get("evidence_node_type"),
        row.get("source_ref_json"),
    ]
    digest = hashlib.sha256("|".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()[:24]
    return f"aep_{digest}"


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=SCHEMA_MIGRATION_ID,
        description="Create typed final-action-to-evidence provenance rows.",
        statements=SCHEMA_STATEMENTS,
        owner="advisory.action_evidence_provenance",
        metadata={"tables": [TABLE_NAME], "authority": "research_only_no_broker_authority"},
    )


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
            retries=2,
        )
    except Exception as exc:
        _record_fallback(
            fallback_type="action_evidence_provenance_schema_lookup_failed",
            source=table_name,
            reason="Action evidence provenance treated source table as unavailable because schema lookup failed.",
            error=exc,
        )
        return set()
    return set(df["column_name"].astype(str).tolist()) if not df.empty else set()


def load_action_rows(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    limit: int = DEFAULT_LIMIT,
) -> pd.DataFrame:
    if not table_exists(ACTION_RECOMMENDATIONS_TABLE):
        return pd.DataFrame()
    available = _table_columns(ACTION_RECOMMENDATIONS_TABLE)
    required = {"asof_date", "symbol", "action_code", "recommendation_reason_json"}
    if not required.issubset(available):
        _record_fallback(
            fallback_type="action_evidence_provenance_required_columns_missing",
            source=ACTION_RECOMMENDATIONS_TABLE,
            reason="Action evidence provenance returned no rows because required action columns were missing.",
            metadata={"missing_columns": sorted(required - available)},
        )
        return pd.DataFrame()
    wanted = [
        "asof_date",
        "published_on",
        "symbol",
        "setup_id",
        "unique_id",
        "action_code",
        "action_source",
        "recommendation_reason_json",
        "raw_context_json",
        "load_ts",
    ]
    selected = [column for column in wanted if column in available]
    clauses = ["recommendation_reason_json IS NOT NULL", "NULLIF(TRIM(symbol), '') IS NOT NULL"]
    params: list[Any] = []
    if from_date is not None:
        clauses.append("asof_date >= %s")
        params.append(from_date)
    if to_date is not None:
        clauses.append("asof_date <= %s")
        params.append(to_date)
    try:
        return sql_to_df(
            f"""
            SELECT {', '.join(selected)}
            FROM {ACTION_RECOMMENDATIONS_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY asof_date DESC, symbol, action_code, setup_id, unique_id
            LIMIT %s
            """,
            params=tuple([*params, int(limit)]),
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_fallback(
            fallback_type="action_evidence_provenance_action_rows_load_failed",
            source=ACTION_RECOMMENDATIONS_TABLE,
            reason="Action evidence provenance could not load final action rows.",
            error=exc,
            metadata={"from_date": str(from_date), "to_date": str(to_date), "limit": int(limit)},
        )
        raise


def _influence_from_action(action_code: str, node: dict[str, Any]) -> tuple[str, str]:
    action = str(action_code or "").upper()
    serialized = json.dumps(node, default=str).lower()
    node_type = str(node.get("claim_role") or node.get("claim_key") or "").lower()
    if action in {"BUY", "BUY_MORE", "WATCH"}:
        default_direction = "supportive"
    elif action in {"SELL", "PARTIAL_SELL", "TIGHTEN_STOP", "REDUCE_EXPOSURE_REVIEW"}:
        default_direction = "risk_reduction"
    elif action == "MANUAL_REVIEW":
        default_direction = "ambiguous_review"
    else:
        default_direction = "neutral"
    if "risk" in node_type or "risk_flag" in serialized:
        return ("conflicting_or_risk", "explain_only_no_ranking_change")
    if "wait_condition" in node_type or "operator_question" in node_type or "future_evidence_to_wait_for" in serialized:
        return ("conditional_or_review", "explain_only_no_ranking_change")
    if "conflict" in serialized or "negative" in serialized or "veto" in serialized:
        return ("conflicting_or_risk", "explain_only_no_ranking_change")
    if "support" in serialized or "positive" in serialized or "candidate_helpful" in serialized:
        return ("supportive", "explain_only_no_ranking_change")
    return (default_direction, "explain_only_no_ranking_change")


def _source_refs(section: str, node: dict[str, Any]) -> dict[str, Any]:
    source_pointers = node.get("source_pointers")
    if not isinstance(source_pointers, list):
        source_pointers = _source_pointer_list(node)
    context_overlay_id = node.get("context_overlay_id") or node.get("overlay_id")
    refs = {
        "section": section,
        "source_section": node.get("source_section"),
        "claim_key": node.get("claim_key"),
        "claim_index": node.get("claim_index"),
        "prompt_id": node.get("llm_prompt_id") or node.get("prompt_id"),
        "prompt_version": node.get("llm_prompt_version") or node.get("prompt_version"),
        "policy_class": node.get("policy_class"),
        "event_class": node.get("event_class"),
        "context_overlay_id": context_overlay_id,
        "memory_id": node.get("memory_id"),
        "signal_id": node.get("signal_id"),
        "conflict_rule_id": node.get("conflict_precedence_rule_id"),
        "source_pointers": source_pointers[:20],
        "source_pointer_count": len(source_pointers),
    }
    return {key: value for key, value in refs.items() if value not in (None, "", [], {})}


def _source_pointer_key(pointer: dict[str, Any]) -> str:
    return json.dumps(pointer, ensure_ascii=False, sort_keys=True, default=str)


def _append_source_pointer(pointers: list[dict[str, Any]], pointer: dict[str, Any]) -> None:
    clean = {key: value for key, value in pointer.items() if value not in (None, "", [], {})}
    if clean:
        pointers.append(clean)


def _source_pointer_list(value: Any, *, _depth: int = 0) -> list[dict[str, Any]]:
    if _depth > 5:
        return []
    pointers: list[dict[str, Any]] = []
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith(("{", "[")):
            return []
        parsed = _parse_jsonish(text, default=None)
        if isinstance(parsed, (dict, list)):
            return _source_pointer_list(parsed, _depth=_depth + 1)
        return []
    if isinstance(value, list):
        for item in value:
            pointers.extend(_source_pointer_list(item, _depth=_depth + 1))
    elif isinstance(value, dict):
        source_table = _clean_text(
            value.get("source_table")
            or value.get("match_source_table")
            or value.get("signal_source_table")
            or value.get("manual_review_source_table")
            or value.get("playbook_source_table")
        )
        source_key = _clean_text(
            value.get("source_key")
            or value.get("match_source_key")
            or value.get("signal_source_key")
            or value.get("manual_review_source_key")
            or value.get("playbook_source_key")
            or value.get("event_unique_id")
        )
        if source_table or source_key:
            _append_source_pointer(
                pointers,
                {
                    "reference_type": "source_row",
                    "source_table": source_table,
                    "source_key": source_key,
                    "source": value.get("source"),
                    "purpose": value.get("purpose"),
                    "status": value.get("status"),
                    "row_count": value.get("row_count"),
                },
            )
        if value.get("memory_id"):
            _append_source_pointer(
                pointers,
                {
                    "reference_type": "causal_memory_row",
                    "source_table": "advisory_causal_event_memory",
                    "source_key": value.get("memory_id"),
                    "memory_id": value.get("memory_id"),
                    "context_source": value.get("context_source"),
                    "event_class": value.get("event_class"),
                    "event_type": value.get("event_type"),
                },
            )
        if value.get("signal_id"):
            _append_source_pointer(
                pointers,
                {
                    "reference_type": "signal_row",
                    "source_table": value.get("signal_source_table") or "advisory_wait_signals",
                    "source_key": value.get("signal_id"),
                    "signal_id": value.get("signal_id"),
                },
            )
        context_overlay_id = value.get("context_overlay_id") or value.get("overlay_id")
        if context_overlay_id:
            context_source = _clean_text(value.get("context_source") or value.get("source") or value.get("source_family"))
            source_table = CONTEXT_OVERLAY_SOURCE_TABLES.get(str(context_source or "").strip().lower())
            _append_source_pointer(
                pointers,
                {
                    "reference_type": "context_overlay_row",
                    "source_table": source_table,
                    "source_key": context_overlay_id,
                    "context_overlay_id": context_overlay_id,
                    "overlay_id": value.get("overlay_id"),
                    "context_source": context_source,
                },
            )
        elif value.get("source") or value.get("context_source"):
            context_source = _clean_text(value.get("context_source") or value.get("source") or value.get("source_family"))
            source_table = CONTEXT_OVERLAY_SOURCE_TABLES.get(str(context_source or "").strip().lower())
            if source_table and (value.get("asof_date") or value.get("class") or value.get("direction")):
                source_key = "|".join(
                    str(part)
                    for part in [
                        context_source or "",
                        value.get("asof_date") or "",
                        value.get("class") or value.get("context_class") or "",
                        value.get("direction") or "",
                    ]
                    if str(part or "").strip()
                )
                _append_source_pointer(
                    pointers,
                    {
                        "reference_type": "context_overlay_query",
                        "source_table": source_table,
                        "source_key": source_key,
                        "context_source": context_source,
                        "context_class": value.get("class") or value.get("context_class"),
                        "direction": value.get("direction"),
                        "asof_date": value.get("asof_date"),
                        "match_precision": "source_date_class_direction",
                    },
                )
        object_key = _clean_text(
            value.get("object_key")
            or value.get("s3_key")
            or value.get("storage_key")
            or value.get("storage_uri")
            or value.get("document_url")
            or value.get("raw_text_s3_key")
            or value.get("text_s3_key")
        )
        if object_key:
            _append_source_pointer(
                pointers,
                {
                    "reference_type": "object_pointer",
                    "object_key": object_key,
                    "storage_form": value.get("announcement_storage_form") or value.get("storage_form"),
                    "source": value.get("source"),
                },
            )
        evidence_contract = value.get("evidence_source_contract")
        if isinstance(evidence_contract, dict):
            for item in evidence_contract.get("sources") or []:
                if isinstance(item, dict):
                    _append_source_pointer(
                        pointers,
                        {
                            "reference_type": "evidence_source_contract",
                            "source": item.get("source"),
                            "source_table": item.get("source_table"),
                            "purpose": item.get("purpose"),
                            "status": item.get("status"),
                            "row_count": item.get("row_count"),
                            "required_for_confident_upgrade": item.get("required_for_confident_upgrade"),
                        },
                    )
        source_refs = value.get("source_refs_json") or value.get("source_refs")
        parsed_source_refs = source_refs
        if isinstance(source_refs, str) and source_refs.strip().startswith(("{", "[")):
            parsed_source_refs = _parse_jsonish(source_refs, default=None)
        if isinstance(parsed_source_refs, (dict, list)):
            pointers.extend(_source_pointer_list(parsed_source_refs, _depth=_depth + 1))
        for item in value.values():
            if isinstance(item, (dict, list)):
                pointers.extend(_source_pointer_list(item, _depth=_depth + 1))
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pointer in pointers:
        key = _source_pointer_key(pointer)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(pointer)
    return deduped


def _as_text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := _clean_text(item))]
    if isinstance(value, tuple):
        return [text for item in value if (text := _clean_text(item))]
    text = _clean_text(value)
    return [text] if text else []


def _claim_node(
    *,
    source_section: str,
    claim_key: str,
    text: str,
    claim_index: int = 0,
    parent: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parent = parent or {}
    node = {
        "source_section": source_section,
        "claim_key": claim_key,
        "claim_index": int(claim_index),
        "text": text,
        "policy_effect": "provenance_audit_only_no_trade_authority",
        "authority_scope": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "llm_prompt_id": parent.get("llm_prompt_id") or parent.get("prompt_id"),
        "llm_prompt_version": parent.get("llm_prompt_version") or parent.get("prompt_version"),
        "policy_class": parent.get("policy_class"),
        "event_class": parent.get("event_class"),
        "memory_id": parent.get("memory_id"),
        "signal_id": parent.get("signal_id"),
        "source_pointers": _source_pointer_list(parent)[:20],
    }
    if extra:
        node.update(extra)
    return {key: value for key, value in node.items() if value not in (None, "", [], {})}


def _add_claim_nodes(
    nodes: list[tuple[str, str, dict[str, Any]]],
    *,
    source_section: str,
    output_section: str,
    node_type: str,
    claim_key: str,
    values: Any,
    parent: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> None:
    for idx, text in enumerate(_as_text_list(values)):
        nodes.append(
            (
                output_section,
                node_type,
                _claim_node(
                    source_section=source_section,
                    claim_key=claim_key,
                    claim_index=idx,
                    text=text,
                    parent=parent,
                    extra=extra,
                ),
            )
        )


def _compact_claim_nodes(section: str, node: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    nodes: list[tuple[str, str, dict[str, Any]]] = []
    if section == "event_policy":
        _add_claim_nodes(
            nodes,
            source_section=section,
            output_section="event_policy_claim",
            node_type="llm_extracted_claim",
            claim_key="operator_summary",
            values=node.get("operator_summary"),
            parent=node,
            extra={"claim_role": "what_happened_or_why_review"},
        )
        _add_claim_nodes(
            nodes,
            source_section=section,
            output_section="event_policy_possible_action",
            node_type="llm_possible_action",
            claim_key="possible_action",
            values=node.get("possible_action"),
            parent=node,
            extra={"claim_role": "suggested_non_executable_action"},
        )
        _add_claim_nodes(
            nodes,
            source_section=section,
            output_section="event_policy_wait_condition",
            node_type="llm_wait_condition",
            claim_key="wait_for_events",
            values=node.get("wait_for_events"),
            parent=node,
            extra={"claim_role": "future_evidence_to_wait_for"},
        )
        _add_claim_nodes(
            nodes,
            source_section=section,
            output_section="event_policy_operator_question",
            node_type="llm_operator_question",
            claim_key="operator_questions",
            values=node.get("operator_questions"),
            parent=node,
            extra={"claim_role": "manual_or_future_research_question"},
        )
    elif section == "company_memory":
        review = node.get("company_memory_review") if isinstance(node.get("company_memory_review"), dict) else node
        _add_claim_nodes(
            nodes,
            source_section=section,
            output_section="company_memory_claim",
            node_type="llm_memory_claim",
            claim_key="summary",
            values=review.get("summary"),
            parent=review,
            extra={"claim_role": "memory_summary"},
        )
        _add_claim_nodes(
            nodes,
            source_section=section,
            output_section="company_memory_claim",
            node_type="llm_memory_claim",
            claim_key="thesis",
            values=review.get("thesis"),
            parent=review,
            extra={"claim_role": "memory_thesis"},
        )
        _add_claim_nodes(
            nodes,
            source_section=section,
            output_section="company_memory_risk_flag",
            node_type="llm_memory_risk_flag",
            claim_key="risk_flags",
            values=review.get("risk_flags"),
            parent=review,
            extra={"claim_role": "memory_risk"},
        )
        _add_claim_nodes(
            nodes,
            source_section=section,
            output_section="company_memory_evidence_reference",
            node_type="llm_memory_evidence_reference",
            claim_key="evidence_used",
            values=review.get("evidence_used"),
            parent=review,
            extra={"claim_role": "retrieved_or_used_memory"},
        )
        _add_claim_nodes(
            nodes,
            source_section=section,
            output_section="company_memory_wait_condition",
            node_type="llm_memory_wait_condition",
            claim_key="wait_for",
            values=review.get("wait_for"),
            parent=review,
            extra={"claim_role": "future_evidence_to_wait_for"},
        )
    elif section == "playbook":
        _add_claim_nodes(
            nodes,
            source_section=section,
            output_section="playbook_claim",
            node_type="llm_playbook_claim",
            claim_key="operator_summary",
            values=node.get("operator_summary"),
            parent=node,
            extra={"claim_role": "playbook_summary"},
        )
        _add_claim_nodes(
            nodes,
            source_section=section,
            output_section="playbook_claim",
            node_type="llm_playbook_claim",
            claim_key="decision_reason",
            values=node.get("decision_reason"),
            parent=node,
            extra={"claim_role": "playbook_decision_reason"},
        )
        _add_claim_nodes(
            nodes,
            source_section=section,
            output_section="playbook_possible_action",
            node_type="llm_playbook_possible_action",
            claim_key="action_type",
            values=node.get("action_type"),
            parent=node,
            extra={"claim_role": "suggested_non_executable_action"},
        )
    return nodes


def _evidence_nodes(contract: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    evidence = contract.get("evidence")
    if not isinstance(evidence, dict):
        return []
    nodes: list[tuple[str, str, dict[str, Any]]] = []
    section_types = {
        "event_policy": "llm_event_policy",
        "playbook": "llm_playbook_plan",
        "company_memory": "llm_company_memory_review",
        "context_overlays": "context_overlay_alignment",
        "causal_event_memory": "causal_event_memory_alignment",
        "conflict_resolution": "deterministic_conflict_resolution",
        "action_transition": "deterministic_transition_contract",
        "manual_review": "manual_review_boundary",
        "signal_refresh": "fast_signal_refresh_review_input",
        "feature_freshness": "feature_freshness_gate",
        "technical": "technical_signal",
        "risk": "risk_contract",
        "macro_regime": "market_context_diagnostic",
    }
    for section, node_type in section_types.items():
        node = evidence.get(section)
        if isinstance(node, dict) and node:
            nodes.append((section, node_type, node))
            nodes.extend(_compact_claim_nodes(section, node))
    return nodes


def build_provenance_rows(actions: pd.DataFrame) -> pd.DataFrame:
    if actions.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    load_ts = pd.Timestamp.utcnow()
    for _, action_row in actions.iterrows():
        contract = _parse_jsonish(action_row.get("recommendation_reason_json"), default={})
        if not isinstance(contract, dict):
            continue
        action_code = str(action_row.get("action_code") or contract.get("action_code") or "").upper()
        action_node = {
            "node_type": "final_action",
            "status": contract.get("status"),
            "action_code": action_code,
            "action_source": action_row.get("action_source") or contract.get("action_source"),
            "setup_id": action_row.get("setup_id") or contract.get("setup_id"),
            "unique_id": action_row.get("unique_id") or contract.get("unique_id"),
            "primary_reason": contract.get("primary_reason"),
            "authority_scope": "advisory_action_recommendation",
            "broker_execution_allowed": False,
        }
        for section, node_type, evidence_node in _evidence_nodes(contract):
            influence_direction, influence_effect = _influence_from_action(action_code, evidence_node)
            row = {
                "asof_date": pd.to_datetime(action_row.get("asof_date"), utc=True, errors="coerce"),
                "published_on": pd.to_datetime(action_row.get("published_on"), utc=True, errors="coerce"),
                "symbol": str(action_row.get("symbol") or "").strip().upper(),
                "setup_id": _clean_text(action_row.get("setup_id") or contract.get("setup_id")),
                "unique_id": _clean_text(action_row.get("unique_id") or contract.get("unique_id")),
                "action_code": action_code,
                "action_source": _clean_text(action_row.get("action_source") or contract.get("action_source")),
                "evidence_section": section,
                "evidence_node_type": node_type,
                "evidence_status": _clean_text(evidence_node.get("status") or evidence_node.get("precondition_status") or contract.get("status")) or "present",
                "influence_direction": influence_direction,
                "influence_effect": influence_effect,
                "source_ref_json": _json_dumps(_source_refs(section, evidence_node)),
                "evidence_node_json": _json_dumps(evidence_node),
                "action_node_json": _json_dumps(action_node),
                "authority_scope": "research_only",
                "policy_effect": "provenance_audit_only_no_trade_authority",
                "broker_execution_allowed": False,
                "policy_auto_promotion_allowed": False,
                "load_ts": load_ts,
            }
            row["provenance_id"] = _provenance_id(row)
            rows.append(row)
    return normalize_provenance_frame(pd.DataFrame(rows))


def normalize_provenance_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for column in ["asof_date", "published_on", "load_ts"]:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    for column in ["broker_execution_allowed", "policy_auto_promotion_allowed"]:
        if column in out.columns:
            out[column] = out[column].map(_bool_or_false).astype("boolean")
    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype("string").str.strip().str.upper()
    return out.dropna(subset=["provenance_id", "asof_date", "symbol", "action_code", "evidence_section"])


def build_action_evidence_provenance(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    end = pd.to_datetime(to_date or pd.Timestamp.utcnow(), utc=True).normalize()
    start = pd.to_datetime(from_date or (end - pd.Timedelta(days=DEFAULT_LOOKBACK_DAYS)), utc=True).normalize()
    actions = load_action_rows(from_date=start, to_date=end, limit=max(1, int(limit)))
    provenance = build_provenance_rows(actions)
    by_section = provenance["evidence_section"].value_counts(dropna=False).to_dict() if not provenance.empty else {}
    by_node_type = provenance["evidence_node_type"].value_counts(dropna=False).to_dict() if not provenance.empty else {}
    meta = {
        "status": "ok",
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "action_rows": int(len(actions)),
        "provenance_rows": int(len(provenance)),
        "evidence_section_counts": {str(key): int(value) for key, value in by_section.items()},
        "evidence_node_type_counts": {str(key): int(value) for key, value in by_node_type.items()},
        "table": TABLE_NAME,
        "authority_scope": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }
    return provenance, meta


def persist_provenance(df: pd.DataFrame) -> None:
    if df.empty:
        return
    ensure_table()
    normalized = normalize_provenance_frame(df)
    if normalized.empty:
        return
    for column in ["setup_id", "unique_id"]:
        if column not in normalized.columns:
            normalized[column] = None
    action_keys = (
        normalized[["asof_date", "symbol", "setup_id", "unique_id", "action_code"]]
        .drop_duplicates()
        .astype(object)
        .where(pd.notna(normalized[["asof_date", "symbol", "setup_id", "unique_id", "action_code"]]), None)
    )

    def _delete_existing() -> int:
        deleted = 0
        with db_session() as (_, cur):
            for row in action_keys.to_dict(orient="records"):
                cur.execute(
                    f"""
                    DELETE FROM {TABLE_NAME}
                    WHERE asof_date = %s
                      AND symbol = %s
                      AND COALESCE(setup_id, '') = COALESCE(%s, '')
                      AND COALESCE(unique_id, '') = COALESCE(%s, '')
                      AND action_code = %s
                    """,
                    (row.get("asof_date"), row.get("symbol"), row.get("setup_id"), row.get("unique_id"), row.get("action_code")),
                )
                deleted += int(cur.rowcount or 0)
        return deleted

    execute_db_operation(_delete_existing, operation_name="action_evidence_provenance:delete_rebuilt_action_keys")
    upsert_to_db(normalized, TABLE_NAME, unique_keys=["provenance_id"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build typed final-action-to-evidence provenance rows.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    provenance, meta = build_action_evidence_provenance(
        from_date=args.from_date,
        to_date=args.to_date,
        limit=max(1, int(args.limit)),
    )
    if not args.dry_run:
        persist_provenance(provenance)
    payload = {
        **meta,
        "persisted": not bool(args.dry_run),
        "sample": provenance.head(10).to_dict(orient="records") if not provenance.empty else [],
    }
    if args.format == "text":
        print(
            f"status={payload['status']} rows={payload['provenance_rows']} persisted={payload['persisted']} "
            f"from={payload['from_date']} to={payload['to_date']}"
        )
        print(f"section_counts={payload['evidence_section_counts']}")
        print(f"node_type_counts={payload['evidence_node_type_counts']}")
    else:
        print(json.dumps(_json_clean(payload), ensure_ascii=False, sort_keys=True, allow_nan=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
