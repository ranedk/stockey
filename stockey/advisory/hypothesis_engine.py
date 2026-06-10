from __future__ import annotations

import argparse
import json
import re
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from environs import Env
from pydantic import BaseModel, Field

from advisory.fallback_telemetry import record_fallback_event
from advisory.market_context import load_latest_market_context
from advisory.prompt_registry import prompt_version as registry_prompt_version
from advisory.prompt_registry import response_schema_version
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg
from utils.codex_cli import run_codex_structured


HYPOTHESES_TABLE = "advisory_hypotheses"
MATCHES_TABLE = "advisory_hypothesis_matches"
ACTION_PLANS_TABLE = "advisory_playbook_action_plans"
PROMOTION_AUDITS_TABLE = "advisory_playbook_promotion_audits"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HYPOTHESES_CONFIG = REPO_ROOT / "config" / "hypotheses.yaml"

DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_PROMOTION_AUDIT_DAYS = 365
DEFAULT_PROMOTION_MIN_MATCHES = 3
DEFAULT_PROMOTION_HORIZONS = [1, 3, 5, 10, 20]
ACTIVE_REVIEW_STATUS = "active_review"
TRUSTED_OVERLAY_STATUS = "trusted_overlay"
PAUSED_STATUS = "paused"
RETIRED_STATUS = "retired"
TRUSTED_OVERLAY_STATUSES = {TRUSTED_OVERLAY_STATUS, "production"}
ACTIVE_SCAN_STATUSES = {"testing", "validated", ACTIVE_REVIEW_STATUS, TRUSTED_OVERLAY_STATUS, "production"}
WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-']+", re.IGNORECASE)
env = Env()
env.read_env()
_TABLES_READY = False
DEFAULT_PLAYBOOK_ACTION_MODEL = env("PLAYBOOK_ACTION_MODEL", default="codex")
PLAYBOOK_ACTION_PROMPT_ID = "playbook_action_plan"
PLAYBOOK_ACTION_PROMPT_VERSION = registry_prompt_version(PLAYBOOK_ACTION_PROMPT_ID)
PLAYBOOK_ACTION_PROMPT_SCHEMA_VERSION = response_schema_version(PLAYBOOK_ACTION_PROMPT_ID)
WEAK_MARKET_BREADTH_THRESHOLD = 45.0
RISK_OFF_STATES = {"HIGH", "STRESS", "RISK_OFF", "DEFENSIVE"}
POSITIVE_PLAYBOOK_ACTIONS = {"BUY", "BUY_MORE", "BUY_WATCH", "WATCH_SYMBOLS", "ADD_TO_WATCHLIST"}
NEGATIVE_PLAYBOOK_ACTIONS = {"REDUCE_EXPOSURE_REVIEW", "GO_CASH_REVIEW", "SHORT_RESEARCH_ONLY", "FULL_EXIT", "PARTIAL_EXIT"}


class PlaybookCheck(BaseModel):
    check_type: str = Field(description="One short category such as price, macro, event, portfolio, liquidity, contradiction, or execution.")
    question: str = Field(description="Specific question the system/operator should answer before acting.")
    data_sources: list[str] = Field(default_factory=list, description="Concrete project data sources or tables to inspect.")
    blocking: bool = Field(default=False, description="Whether action should wait for this check.")
    rationale: str = Field(default="", description="Why this check matters.")


class PlaybookActionPlan(BaseModel):
    action_type: str = Field(description="Recommended action class such as MANUAL_REVIEW, REDUCE_EXPOSURE_REVIEW, SECTOR_REVIEW, GO_CASH_REVIEW, WATCH_SYMBOLS, or NO_ACTION.")
    urgency: str = Field(default="normal", description="low, normal, high, or immediate.")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    production_allowed: bool = Field(default=False)
    operator_summary: str = Field(description="Plain-English operator summary.")
    decision_reason: str = Field(description="Why this action is appropriate for the matched playbook evidence.")
    checks: list[PlaybookCheck] = Field(default_factory=list)
    risk_controls: dict[str, Any] = Field(default_factory=dict)
    follow_up_window_days: int = Field(default=3, ge=0)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


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


def ensure_tables(*, force: bool = False) -> None:
    global _TABLES_READY
    if _TABLES_READY and not force:
        return
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {HYPOTHESES_TABLE} (
                hypothesis_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                source TEXT,
                status TEXT,
                trigger_scope TEXT,
                trigger_patterns_json TEXT,
                expected_effect_json TEXT,
                holding_window_days BIGINT,
                decision_policy_json TEXT,
                created_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ,
                load_ts TIMESTAMPTZ
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MATCHES_TABLE} (
                matched_at TIMESTAMPTZ NOT NULL,
                hypothesis_id TEXT NOT NULL,
                hypothesis_title TEXT,
                status TEXT,
                source_type TEXT NOT NULL,
                source_table TEXT NOT NULL,
                source_key TEXT NOT NULL,
                published_on TIMESTAMPTZ,
                symbol TEXT,
                subject TEXT,
                source_url TEXT,
                match_score DOUBLE PRECISION,
                matched_terms_json TEXT,
                evidence_text TEXT,
                expected_effect_json TEXT,
                suggested_action TEXT,
                action_reason TEXT,
                decision_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (hypothesis_id, source_table, source_key)
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {ACTION_PLANS_TABLE} (
                planned_at TIMESTAMPTZ NOT NULL,
                hypothesis_id TEXT NOT NULL,
                source_table TEXT NOT NULL,
                source_key TEXT NOT NULL,
                symbol TEXT,
                trigger_scope TEXT,
                suggested_action TEXT,
                action_type TEXT,
                urgency TEXT,
                confidence DOUBLE PRECISION,
                production_allowed BOOLEAN,
                operator_summary TEXT,
                decision_reason TEXT,
                checks_json TEXT,
                risk_controls_json TEXT,
                action_plan_json TEXT,
                market_context_json TEXT,
                market_context_adjustment_json TEXT,
                market_context_adjustment TEXT,
                prompt_id TEXT,
                prompt_version TEXT,
                prompt_schema_version TEXT,
                llm_model TEXT,
                llm_status TEXT,
                llm_error TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (hypothesis_id, source_table, source_key)
            )
            """
        )
        for column, sql_type in {
            "prompt_id": "TEXT",
            "prompt_version": "TEXT",
            "prompt_schema_version": "TEXT",
        }.items():
            cur.execute(f"ALTER TABLE {ACTION_PLANS_TABLE} ADD COLUMN IF NOT EXISTS {column} {sql_type}")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {PROMOTION_AUDITS_TABLE} (
                audited_at TIMESTAMPTZ NOT NULL,
                hypothesis_id TEXT NOT NULL,
                audit_status TEXT NOT NULL,
                match_count BIGINT,
                source_type_count BIGINT,
                symbol_count BIGINT,
                first_match_at TIMESTAMPTZ,
                last_match_at TIMESTAMPTZ,
                min_matches_required BIGINT,
                lookback_days BIGINT,
                audit_reason TEXT,
                evidence_json TEXT,
                operator_notes TEXT,
                approved_by TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (hypothesis_id, audited_at)
            )
            """
        )
    _TABLES_READY = True


def normalize_text(value: Any) -> str:
    return " ".join(match.group(0).lower() for match in WORD_RE.finditer(str(value or "")))


def normalize_terms(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_terms = re.split(r"[,;\n]+", value)
    elif isinstance(value, dict):
        raw_terms = []
        for key in ["keywords", "phrases", "entities", "authority_roles", "required_terms"]:
            item = value.get(key)
            if isinstance(item, list):
                raw_terms.extend(item)
            elif isinstance(item, str):
                raw_terms.extend(re.split(r"[,;\n]+", item))
    elif isinstance(value, list):
        raw_terms = value
    else:
        raw_terms = [value]
    terms = []
    for term in raw_terms:
        normalized = normalize_text(term)
        if normalized and normalized not in terms:
            terms.append(normalized)
    return terms


def parse_jsonish(value: Any, default: Any) -> Any:
    if value is None or (not isinstance(value, (dict, list)) and pd.isna(value)):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _clean_json_record(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
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


def load_playbook_market_context(match: pd.Series) -> dict[str, Any]:
    published_on = pd.to_datetime(match.get("published_on") or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(published_on):
        published_on = pd.Timestamp.utcnow()
    try:
        payload = load_latest_market_context(published_on.normalize(), limit=250)
    except Exception as exc:
        return {
            "summary": {},
            "symbol_context": {},
            "error": f"{type(exc).__name__}: {exc}",
        }
    summary = payload.get("summary") or {}
    top_universe = payload.get("top_universe") or []
    symbol = str(match.get("symbol") or "").strip().upper()
    symbol_context = {}
    if symbol:
        for row in top_universe:
            if str(row.get("symbol") or "").strip().upper() == symbol:
                symbol_context = row
                break
    return {
        "summary": _clean_json_record(summary) if isinstance(summary, dict) else {},
        "symbol_context": _clean_json_record(symbol_context) if isinstance(symbol_context, dict) else {},
    }


def market_context_adjustment(action_type: str, market_context: dict[str, Any]) -> dict[str, Any]:
    summary = market_context.get("summary") if isinstance(market_context, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    action = str(action_type or "").strip().upper()
    regime_name = str(summary.get("regime_name") or "").strip().upper()
    macro_risk_state = str(summary.get("macro_risk_state") or "").strip().upper()
    breadth = pd.to_numeric(summary.get("breadth_trend_alignment_pct"), errors="coerce")
    risk_off_score = pd.to_numeric(summary.get("risk_off_score"), errors="coerce")
    weak_breadth = bool(not pd.isna(breadth) and float(breadth) < WEAK_MARKET_BREADTH_THRESHOLD)
    risk_off = regime_name in RISK_OFF_STATES or macro_risk_state in RISK_OFF_STATES or bool(not pd.isna(risk_off_score) and float(risk_off_score) >= 0.60)
    adjusted_action = action
    adjustment = "none"
    reason = "Market context did not change the playbook action boundary."
    production_allowed_override: bool | None = None
    urgency_override: str | None = None
    confidence_delta = 0.0

    if action in POSITIVE_PLAYBOOK_ACTIONS and (risk_off or weak_breadth):
        adjusted_action = "BUY_WATCH"
        adjustment = "positive_event_downgraded_by_market_context"
        production_allowed_override = False
        urgency_override = "normal"
        confidence_delta = -0.10
        reason = (
            "Positive playbook evidence was downgraded to watch/manual review because broad market context is weak "
            f"(regime={regime_name or 'n/a'}, macro_risk={macro_risk_state or 'n/a'}, "
            f"trend_breadth={None if pd.isna(breadth) else round(float(breadth), 2)}%)."
        )
    elif action in NEGATIVE_PLAYBOOK_ACTIONS and (risk_off or weak_breadth):
        adjustment = "risk_reduction_reinforced_by_market_context"
        urgency_override = "high"
        confidence_delta = 0.10
        reason = (
            "Risk-reduction playbook evidence is reinforced by weak/risk-off broad market context "
            f"(regime={regime_name or 'n/a'}, macro_risk={macro_risk_state or 'n/a'}, "
            f"trend_breadth={None if pd.isna(breadth) else round(float(breadth), 2)}%)."
        )

    return {
        "adjustment": adjustment,
        "original_action_type": action,
        "adjusted_action_type": adjusted_action,
        "reason": reason,
        "regime_name": regime_name or None,
        "macro_risk_state": macro_risk_state or None,
        "breadth_trend_alignment_pct": None if pd.isna(breadth) else float(breadth),
        "risk_off_score": None if pd.isna(risk_off_score) else float(risk_off_score),
        "production_allowed_override": production_allowed_override,
        "urgency_override": urgency_override,
        "confidence_delta": confidence_delta,
    }


def make_hypothesis_id(title: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "_", str(title).upper()).strip("_")[:56] or "HYPOTHESIS"
    return f"{slug}_{uuid.uuid4().hex[:8].upper()}"


def _boolish(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def normalize_playbook_status(value: Any) -> str:
    normalized = str(value or ACTIVE_REVIEW_STATUS).strip().lower()
    aliases = {
        "testing": ACTIVE_REVIEW_STATUS,
        "validated": ACTIVE_REVIEW_STATUS,
        "production": TRUSTED_OVERLAY_STATUS,
        "pause": PAUSED_STATUS,
        "disabled": PAUSED_STATUS,
        "rejected": RETIRED_STATUS,
        "inactive": RETIRED_STATUS,
    }
    return aliases.get(normalized, normalized or ACTIVE_REVIEW_STATUS)


def is_trusted_overlay_status(value: Any) -> bool:
    return normalize_playbook_status(value) == TRUSTED_OVERLAY_STATUS or str(value or "").strip().lower() in TRUSTED_OVERLAY_STATUSES


def latest_promotion_audit(hypothesis_id: str) -> dict[str, Any] | None:
    ensure_tables()
    df = sql_to_df(
        f"""
        SELECT *
        FROM {PROMOTION_AUDITS_TABLE}
        WHERE hypothesis_id = %s
        ORDER BY audited_at DESC
        LIMIT 1
        """,
        params=(str(hypothesis_id),),
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def latest_promotion_audits(hypothesis_ids: list[str]) -> list[dict[str, Any]]:
    ensure_tables()
    normalized = [str(value).strip() for value in hypothesis_ids if str(value or "").strip()]
    if not normalized:
        return []
    df = sql_to_df(
        f"""
        SELECT DISTINCT ON (hypothesis_id) *
        FROM {PROMOTION_AUDITS_TABLE}
        WHERE hypothesis_id = ANY(%s)
        ORDER BY hypothesis_id, audited_at DESC
        """,
        params=(normalized,),
    )
    return df.to_dict(orient="records") if not df.empty else []


def production_gate_passes(hypothesis_id: str, *, allow_override: bool = False) -> bool:
    """Legacy compatibility wrapper.

    Playbooks are qualitative reliability overlays now, not statistically promoted
    trading policies. Existing callers can still inspect the latest reliability
    check, but status changes no longer require this gate.
    """
    if allow_override:
        return True
    latest = latest_promotion_audit(hypothesis_id)
    return bool(latest and str(latest.get("audit_status") or "").lower() in {"approved", "sufficient_history"})


def create_hypothesis(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_tables()
    now = pd.Timestamp.utcnow()
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    trigger_patterns = _normalize_trigger_patterns(payload.get("trigger_patterns"), payload.get("description"))
    expected_effect = payload.get("expected_effect") or {"effect": "manual_review"}
    hypothesis_id = str(payload.get("hypothesis_id") or make_hypothesis_id(title))
    status = normalize_playbook_status(payload.get("status"))
    row = {
        "hypothesis_id": hypothesis_id,
        "title": title,
        "description": str(payload.get("description") or "").strip() or None,
        "source": str(payload.get("source") or "operator").strip() or "operator",
        "status": status,
        "trigger_scope": str(payload.get("trigger_scope") or "market").strip().lower(),
        "trigger_patterns_json": json_dumps(trigger_patterns),
        "expected_effect_json": json_dumps(expected_effect),
        "holding_window_days": int(payload.get("holding_window_days") or DEFAULT_LOOKBACK_DAYS),
        "decision_policy_json": json_dumps(payload.get("decision_policy") or {}),
        "created_at": now,
        "updated_at": now,
        "load_ts": now,
    }
    upsert_to_db(pd.DataFrame([row]), HYPOTHESES_TABLE, unique_keys=["hypothesis_id"])
    return row


def update_hypothesis(hypothesis_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_tables()
    normalized_id = str(hypothesis_id or "").strip()
    if not normalized_id:
        raise ValueError("hypothesis_id is required")
    existing = sql_to_df(
        f"""
        SELECT created_at
        FROM {HYPOTHESES_TABLE}
        WHERE hypothesis_id = %s
        LIMIT 1
        """,
        params=(normalized_id,),
    )
    if existing.empty:
        raise ValueError(f"Unknown hypothesis_id: {normalized_id}")
    effective_payload = {**payload, "hypothesis_id": normalized_id}
    row = create_hypothesis(effective_payload)
    created_at = pd.to_datetime(existing.iloc[0].get("created_at"), utc=True, errors="coerce")
    if not pd.isna(created_at):
        row["created_at"] = created_at
        upsert_to_db(pd.DataFrame([row]), HYPOTHESES_TABLE, unique_keys=["hypothesis_id"])
    return row


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple | set):
        return list(value)
    return [value]


def _normalize_trigger_patterns(raw: Any, description: Any = None) -> dict[str, Any]:
    if raw is None:
        return {"keywords": normalize_terms(description)[:12]}
    if isinstance(raw, str):
        return {"keywords": normalize_terms(raw)}
    if not isinstance(raw, dict):
        return {"keywords": normalize_terms(raw)}

    out = dict(raw)
    keywords = raw.get("keywords")
    if isinstance(keywords, dict):
        include_terms: list[Any] = []
        for key in ["include", "phrases", "entities", "required_terms"]:
            include_terms.extend(_as_list(keywords.get(key)))
        out["keywords"] = normalize_terms(include_terms)
        if "exclude" in keywords:
            out["exclude_keywords"] = normalize_terms(keywords.get("exclude"))
    else:
        out["keywords"] = normalize_terms(keywords)
    for key in ["phrases", "entities", "authority_roles", "required_terms"]:
        if key in raw:
            out[key] = normalize_terms(raw.get(key))
    return out


def normalize_hypothesis_config_entry(raw: dict[str, Any]) -> dict[str, Any]:
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ValueError("hypothesis config entry missing title")
    holding_window = raw.get("holding_window")
    holding_window_days = raw.get("holding_window_days")
    if holding_window_days is None and isinstance(holding_window, dict):
        horizons = pd.to_numeric(pd.Series(_as_list(holding_window.get("horizons_days"))), errors="coerce").dropna()
        holding_window_days = int(horizons.max()) if not horizons.empty else DEFAULT_LOOKBACK_DAYS
    payload = {
        "hypothesis_id": raw.get("hypothesis_id"),
        "title": title,
        "description": raw.get("description"),
        "source": raw.get("source") or "config",
        "status": normalize_playbook_status(raw.get("status")),
        "trigger_scope": raw.get("trigger_scope") or "market",
        "trigger_patterns": _normalize_trigger_patterns(raw.get("trigger_patterns"), raw.get("description")),
        "expected_effect": raw.get("expected_effect") or {"effect": "manual_review"},
        "holding_window_days": holding_window_days or DEFAULT_LOOKBACK_DAYS,
        "decision_policy": raw.get("decision_policy") or {},
    }
    if isinstance(holding_window, dict):
        payload["decision_policy"] = {
            **payload["decision_policy"],
            "holding_window": holding_window,
        }
    for key in ["validation_protocol", "reliability_rule", "promotion_rule", "test_universe", "notes"]:
        if raw.get(key) is not None:
            payload["decision_policy"] = {
                **payload["decision_policy"],
                key: raw.get(key),
            }
    return payload


def load_hypotheses_config(path: str | Path = DEFAULT_HYPOTHESES_CONFIG) -> list[dict[str, Any]]:
    config_path = Path(path)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw_items = payload.get("hypotheses") if isinstance(payload, dict) else payload
    items = _as_list(raw_items)
    return [normalize_hypothesis_config_entry(item) for item in items if isinstance(item, dict)]


def import_hypotheses_config(path: str | Path = DEFAULT_HYPOTHESES_CONFIG, *, dry_run: bool = False) -> dict[str, Any]:
    rows = []
    for item in load_hypotheses_config(path):
        if dry_run:
            title = str(item.get("title") or "").strip()
            row = {
                "hypothesis_id": str(item.get("hypothesis_id") or make_hypothesis_id(title)),
                "title": title,
                "status": normalize_playbook_status(item.get("status")),
                "trigger_scope": str(item.get("trigger_scope") or "market").strip().lower(),
                "trigger_patterns_json": json_dumps(item.get("trigger_patterns")),
                "expected_effect_json": json_dumps(item.get("expected_effect")),
                "decision_policy_json": json_dumps(item.get("decision_policy") or {}),
            }
        else:
            row = create_hypothesis(item)
        rows.append(row)
    return {
        "status": "ok",
        "config_path": str(Path(path)),
        "imported_count": int(len(rows)),
        "dry_run": bool(dry_run),
        "hypotheses": rows,
    }


def preview_hypothesis_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_hypothesis_config_entry(payload)
    title = str(normalized.get("title") or "").strip()
    hypothesis_id = str(normalized.get("hypothesis_id") or make_hypothesis_id(title))
    row = {
        "hypothesis_id": hypothesis_id,
        "title": title,
        "description": normalized.get("description"),
        "source": normalized.get("source") or "operator",
        "status": normalize_playbook_status(normalized.get("status")),
        "trigger_scope": str(normalized.get("trigger_scope") or "market").strip().lower(),
        "trigger_patterns_json": json_dumps(normalized.get("trigger_patterns")),
        "expected_effect_json": json_dumps(normalized.get("expected_effect")),
        "holding_window_days": int(normalized.get("holding_window_days") or DEFAULT_LOOKBACK_DAYS),
        "decision_policy_json": json_dumps(normalized.get("decision_policy") or {}),
    }
    return {
        "status": "ok",
        "normalized_payload": normalized,
        "db_row_preview": row,
        "production_note": (
            "Playbooks are qualitative reliability overlays. Only status=trusted_overlay "
            "can affect consolidated actions, and only as review_only risk overlays."
        ),
    }


def build_promotion_audit(
    hypothesis_id: str,
    *,
    lookback_days: int = DEFAULT_PROMOTION_AUDIT_DAYS,
    min_matches: int = DEFAULT_PROMOTION_MIN_MATCHES,
    operator_notes: str | None = None,
    approved_by: str | None = None,
) -> dict[str, Any]:
    ensure_tables()
    normalized_id = str(hypothesis_id or "").strip()
    if not normalized_id:
        raise ValueError("hypothesis_id is required")
    since = pd.Timestamp.utcnow() - pd.Timedelta(days=max(1, int(lookback_days)))
    matches = sql_to_df(
        f"""
        SELECT *
        FROM {MATCHES_TABLE}
        WHERE hypothesis_id = %s
          AND COALESCE(published_on, matched_at) >= %s
        ORDER BY COALESCE(published_on, matched_at) DESC
        """,
        params=(normalized_id, since),
    )
    match_count = int(len(matches))
    source_type_count = int(matches["source_type"].dropna().astype(str).nunique()) if not matches.empty and "source_type" in matches.columns else 0
    symbol_count = int(matches["symbol"].dropna().astype(str).str.upper().nunique()) if not matches.empty and "symbol" in matches.columns else 0
    first_match_at = None
    last_match_at = None
    if not matches.empty:
        match_times = pd.to_datetime(matches.get("published_on", matches.get("matched_at")), utc=True, errors="coerce").dropna()
        if not match_times.empty:
            first_match_at = match_times.min()
            last_match_at = match_times.max()
    missing: list[str] = []
    if match_count < int(min_matches):
        missing.append(f"needs at least {int(min_matches)} historical match(es)")
    if source_type_count < 1:
        missing.append("needs at least one evidence source type")
    forward_evaluation = evaluate_match_forward_returns(normalized_id, matches)
    if forward_evaluation.get("evaluated_rows", 0) < 1:
        missing.append("needs at least one matured price-linked forward return")
    audit_status = "sufficient_history" if not missing else "insufficient_history"
    audit_reason = "Reliability check has enough historical observations for operator review." if audit_status == "sufficient_history" else "; ".join(missing)
    evidence = {
        "match_count": match_count,
        "source_type_count": source_type_count,
        "symbol_count": symbol_count,
        "source_types": sorted(matches["source_type"].dropna().astype(str).unique().tolist()) if not matches.empty and "source_type" in matches.columns else [],
        "sample_matches": matches.head(10).to_dict(orient="records") if not matches.empty else [],
        "forward_return_status": forward_evaluation.get("status"),
        "forward_return_note": forward_evaluation.get("note"),
        "forward_return_summary": forward_evaluation.get("summary", []),
        "forward_return_rows": forward_evaluation.get("rows", [])[:50],
    }
    now = pd.Timestamp.utcnow()
    row = {
        "audited_at": now,
        "hypothesis_id": normalized_id,
        "audit_status": audit_status,
        "match_count": match_count,
        "source_type_count": source_type_count,
        "symbol_count": symbol_count,
        "first_match_at": first_match_at,
        "last_match_at": last_match_at,
        "min_matches_required": int(min_matches),
        "lookback_days": int(lookback_days),
        "audit_reason": audit_reason,
        "evidence_json": json_dumps(evidence),
        "operator_notes": operator_notes,
        "approved_by": approved_by,
        "load_ts": now,
    }
    return row


def persist_promotion_audit(row: dict[str, Any]) -> None:
    ensure_tables()
    out = pd.DataFrame([row])
    for column in ["audited_at", "first_match_at", "last_match_at", "load_ts"]:
        out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    for column in ["match_count", "source_type_count", "symbol_count", "min_matches_required", "lookback_days"]:
        out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    upsert_to_db(out, PROMOTION_AUDITS_TABLE, unique_keys=["hypothesis_id", "audited_at"])


def run_promotion_audit(
    hypothesis_id: str,
    *,
    lookback_days: int = DEFAULT_PROMOTION_AUDIT_DAYS,
    min_matches: int = DEFAULT_PROMOTION_MIN_MATCHES,
    operator_notes: str | None = None,
    approved_by: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Run a legacy-named playbook reliability check.

    The endpoint/table names remain unchanged for compatibility, but this is no
    longer a statistical production promotion gate for fundamental playbooks.
    """
    row = build_promotion_audit(
        hypothesis_id,
        lookback_days=lookback_days,
        min_matches=min_matches,
        operator_notes=operator_notes,
        approved_by=approved_by,
    )
    if persist:
        persist_promotion_audit(row)
    return {"status": "ok", "audit": row}


def _audit_horizons_for_hypothesis(hypothesis_id: str) -> list[int]:
    try:
        hypotheses = load_hypotheses(include_inactive=True)
    except Exception:
        hypotheses = pd.DataFrame()
    if hypotheses.empty:
        return DEFAULT_PROMOTION_HORIZONS
    row = hypotheses[hypotheses["hypothesis_id"].astype(str) == str(hypothesis_id)]
    if row.empty:
        return DEFAULT_PROMOTION_HORIZONS
    policy = parse_jsonish(row.iloc[0].get("decision_policy_json"), {})
    holding_window = policy.get("holding_window") if isinstance(policy, dict) else {}
    horizons = _as_list(holding_window.get("horizons_days") if isinstance(holding_window, dict) else None)
    parsed = pd.to_numeric(pd.Series(horizons), errors="coerce").dropna().astype(int).tolist()
    return sorted({value for value in parsed if value > 0}) or DEFAULT_PROMOTION_HORIZONS


def _load_audit_price_history(targets: list[str], *, start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.DataFrame:
    symbols = sorted({str(value).strip().upper() for value in targets if str(value or "").strip()})
    if not symbols:
        return pd.DataFrame()
    df = sql_to_df(
        """
        SELECT ticker AS symbol, asset_type, date, close
        FROM dhan_ohlcv_daily
        WHERE exchange = 'NSE'
          AND ticker = ANY(%(symbols)s)
          AND date >= %(start_date)s
          AND date <= %(end_date)s
        ORDER BY ticker, asset_type, date
        """,
        params={"symbols": symbols, "start_date": start_date, "end_date": end_date},
    )
    if df.empty:
        return df
    required_columns = {"symbol", "asset_type", "date", "close"}
    if not required_columns.issubset(set(df.columns)):
        return pd.DataFrame()
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["asset_type"] = df["asset_type"].astype("string").str.strip().str.lower()
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce").dt.normalize()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    priority = {"stock": 0, "benchmark": 1, "index": 2}
    df["asset_priority"] = df["asset_type"].map(priority).fillna(9)
    return (
        df.dropna(subset=["symbol", "date", "close"])
        .sort_values(["symbol", "date", "asset_priority"], kind="stable")
        .drop_duplicates(subset=["symbol", "date"], keep="first")
        .drop(columns=["asset_priority"])
    )


def _load_audit_sector_memberships(anchor_symbols: list[str]) -> dict[str, list[str]]:
    symbols = sorted({str(value).strip().upper() for value in anchor_symbols if str(value or "").strip() and str(value).strip().upper() != "NIFTY"})
    if not symbols:
        return {}
    df = sql_to_df(
        """
        WITH sector_counts AS (
            SELECT
                UPPER(TRIM(symbol)) AS symbol,
                TRIM(sector_code) AS sector_code,
                COUNT(*) AS row_count
            FROM master_sharpely_equity
            WHERE NULLIF(TRIM(symbol), '') IS NOT NULL
              AND NULLIF(TRIM(sector_code), '') IS NOT NULL
            GROUP BY UPPER(TRIM(symbol)), TRIM(sector_code)
        ),
        ranked AS (
            SELECT
                symbol,
                sector_code,
                ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY row_count DESC, sector_code) AS rn
            FROM sector_counts
        ),
        anchors AS (
            SELECT symbol AS anchor_symbol, sector_code
            FROM ranked
            WHERE rn = 1
              AND symbol = ANY(%(symbols)s)
        )
        SELECT
            anchors.anchor_symbol,
            anchors.sector_code,
            ranked.symbol AS member_symbol
        FROM anchors
        JOIN ranked
          ON ranked.rn = 1
         AND ranked.sector_code = anchors.sector_code
         AND ranked.symbol <> anchors.anchor_symbol
        ORDER BY anchors.anchor_symbol, ranked.symbol
        """,
        params={"symbols": symbols},
    )
    if df.empty or not {"anchor_symbol", "member_symbol"}.issubset(set(df.columns)):
        raise ValueError(f"No master sector mappings found for reliability-check symbols: {', '.join(symbols[:20])}")
    df["anchor_symbol"] = df["anchor_symbol"].astype("string").str.strip().str.upper()
    df["member_symbol"] = df["member_symbol"].astype("string").str.strip().str.upper()
    df = df.dropna(subset=["anchor_symbol", "member_symbol"]).drop_duplicates(subset=["anchor_symbol", "member_symbol"])
    sector_map: dict[str, list[str]] = {}
    for anchor, group in df.groupby("anchor_symbol", sort=False):
        members = [str(value) for value in group["member_symbol"].tolist() if str(value or "").strip() and str(value) != str(anchor)]
        if members:
            sector_map[str(anchor)] = members[:100]
    missing = sorted(set(symbols) - set(sector_map))
    if missing:
        raise ValueError(f"Missing master sector mappings for reliability-check symbols: {', '.join(missing[:20])}")
    return sector_map


def _target_symbol_for_match(row: pd.Series) -> str:
    symbol = str(row.get("symbol") or "").strip().upper()
    if symbol:
        return symbol
    return "NIFTY"


def _future_return(price_group: pd.DataFrame | None, event_date: pd.Timestamp, horizon_days: int) -> dict[str, Any]:
    if price_group is None or price_group.empty:
        return {"status": "missing_prices"}
    future = price_group[price_group["date"] > event_date.normalize()].sort_values("date").reset_index(drop=True)
    if future.empty:
        return {"status": "missing_entry"}
    anchor = future.iloc[0]
    if len(future) <= int(horizon_days):
        return {
            "status": "not_matured",
            "anchor_date": anchor.get("date"),
            "anchor_close": float(anchor.get("close")),
        }
    exit_row = future.iloc[int(horizon_days)]
    anchor_close = float(anchor.get("close"))
    exit_close = float(exit_row.get("close"))
    return {
        "status": "evaluated",
        "anchor_date": anchor.get("date"),
        "anchor_close": anchor_close,
        "exit_date": exit_row.get("date"),
        "exit_close": exit_close,
        "forward_return": (exit_close / anchor_close) - 1.0 if anchor_close else None,
    }


def _sector_proxy_return(
    member_symbols: list[str],
    price_groups: dict[str, pd.DataFrame],
    event_date: pd.Timestamp,
    horizon_days: int,
) -> dict[str, Any]:
    if not member_symbols:
        return {"status": "no_sector_members", "member_count": 0}
    returns: list[float] = []
    statuses: list[str] = []
    for member_symbol in member_symbols:
        result = _future_return(price_groups.get(member_symbol), event_date, horizon_days)
        statuses.append(str(result.get("status") or "unknown"))
        forward_return = pd.to_numeric(result.get("forward_return"), errors="coerce")
        if result.get("status") == "evaluated" and pd.notna(forward_return):
            returns.append(float(forward_return))
    if returns:
        return {
            "status": "evaluated",
            "member_count": len(returns),
            "available_member_count": len(member_symbols),
            "forward_return": float(pd.Series(returns).mean()),
        }
    status = "not_matured_or_missing" if any(value in {"not_matured", "missing_prices"} for value in statuses) else "missing_prices"
    return {"status": status, "member_count": 0, "available_member_count": len(member_symbols)}


def _match_event_timestamp(match: pd.Series) -> pd.Timestamp:
    published = pd.to_datetime(match.get("published_on"), utc=True, errors="coerce")
    if not pd.isna(published):
        return published
    return pd.to_datetime(match.get("matched_at"), utc=True, errors="coerce")


def evaluate_match_forward_returns(hypothesis_id: str, matches: pd.DataFrame) -> dict[str, Any]:
    if matches.empty:
        return {"status": "no_matches", "note": "No persisted matches available for forward-return evaluation.", "rows": [], "summary": [], "evaluated_rows": 0}
    horizons = _audit_horizons_for_hypothesis(hypothesis_id)
    event_times = pd.to_datetime(matches.get("published_on", matches.get("matched_at")), utc=True, errors="coerce").dropna()
    if event_times.empty:
        return {"status": "missing_event_dates", "note": "Matches do not have usable published/matched timestamps.", "rows": [], "summary": [], "evaluated_rows": 0}
    targets = [_target_symbol_for_match(row) for _, row in matches.iterrows()]
    sector_map = _load_audit_sector_memberships(targets)
    sector_symbols = sorted({member for members in sector_map.values() for member in members})
    targets.extend(sector_symbols)
    targets.append("NIFTY")
    start_date = event_times.min().normalize()
    end_date = event_times.max().normalize() + pd.Timedelta(days=max(horizons) + 45)
    prices = _load_audit_price_history(targets, start_date=start_date, end_date=end_date)
    if prices.empty:
        return {"status": "missing_prices", "note": "No Dhan daily OHLCV rows found for matched symbols/benchmark.", "rows": [], "summary": [], "evaluated_rows": 0}
    price_groups = {symbol: group.sort_values("date").reset_index(drop=True) for symbol, group in prices.groupby("symbol", sort=False)}
    rows: list[dict[str, Any]] = []
    for _, match in matches.iterrows():
        target_symbol = _target_symbol_for_match(match)
        price_group = price_groups.get(target_symbol)
        if price_group is None or price_group.empty:
            for horizon in horizons:
                rows.append(
                    {
                        "source_key": match.get("source_key"),
                        "symbol": target_symbol,
                        "horizon_days": horizon,
                        "status": "missing_prices",
                    }
                )
            continue
        event_date = _match_event_timestamp(match)
        if pd.isna(event_date):
            continue
        for horizon in horizons:
            result = _future_return(price_group, event_date, int(horizon))
            benchmark_result = _future_return(price_groups.get("NIFTY"), event_date, int(horizon))
            sector_proxy_result = _sector_proxy_return(sector_map.get(target_symbol, []), price_groups, event_date, int(horizon))
            benchmark_return = pd.to_numeric(benchmark_result.get("forward_return"), errors="coerce")
            sector_proxy_return = pd.to_numeric(sector_proxy_result.get("forward_return"), errors="coerce")
            forward_return = pd.to_numeric(result.get("forward_return"), errors="coerce")
            excess_return = None
            if result.get("status") == "evaluated" and benchmark_result.get("status") == "evaluated" and pd.notna(forward_return) and pd.notna(benchmark_return):
                excess_return = float(forward_return) - float(benchmark_return)
            sector_proxy_excess_return = None
            if result.get("status") == "evaluated" and sector_proxy_result.get("status") == "evaluated" and pd.notna(forward_return) and pd.notna(sector_proxy_return):
                sector_proxy_excess_return = float(forward_return) - float(sector_proxy_return)
            rows.append(
                {
                    "source_key": match.get("source_key"),
                    "source_type": match.get("source_type"),
                    "symbol": target_symbol,
                    "event_date": event_date,
                    "horizon_days": int(horizon),
                    **result,
                    "benchmark_symbol": "NIFTY",
                    "benchmark_status": benchmark_result.get("status"),
                    "benchmark_anchor_close": benchmark_result.get("anchor_close"),
                    "benchmark_exit_close": benchmark_result.get("exit_close"),
                    "benchmark_forward_return": None if pd.isna(benchmark_return) else float(benchmark_return),
                    "excess_return_vs_benchmark": excess_return,
                    "sector_proxy_status": sector_proxy_result.get("status"),
                    "sector_proxy_member_count": sector_proxy_result.get("member_count"),
                    "sector_proxy_available_member_count": sector_proxy_result.get("available_member_count"),
                    "sector_proxy_forward_return": None if pd.isna(sector_proxy_return) else float(sector_proxy_return),
                    "excess_return_vs_sector_proxy": sector_proxy_excess_return,
                }
            )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return {"status": "not_evaluable", "note": "No forward-return rows could be constructed.", "rows": [], "summary": [], "evaluated_rows": 0}
    evaluated = frame[frame["status"].astype(str) == "evaluated"].copy()
    if evaluated.empty:
        status_counts = frame["status"].value_counts(dropna=False).to_dict()
        return {
            "status": "not_matured_or_missing",
            "note": f"No matured forward returns yet. Status counts: {status_counts}",
            "rows": frame.to_dict(orient="records"),
            "summary": [],
            "evaluated_rows": 0,
        }
    evaluated["forward_return"] = pd.to_numeric(evaluated["forward_return"], errors="coerce")
    evaluated["benchmark_forward_return"] = pd.to_numeric(evaluated.get("benchmark_forward_return"), errors="coerce")
    evaluated["excess_return_vs_benchmark"] = pd.to_numeric(evaluated.get("excess_return_vs_benchmark"), errors="coerce")
    evaluated["sector_proxy_forward_return"] = pd.to_numeric(evaluated.get("sector_proxy_forward_return"), errors="coerce")
    evaluated["excess_return_vs_sector_proxy"] = pd.to_numeric(evaluated.get("excess_return_vs_sector_proxy"), errors="coerce")
    summary = (
        evaluated.dropna(subset=["forward_return"])
        .groupby("horizon_days", dropna=False)
        .agg(
            evaluated_count=("forward_return", "count"),
            mean_forward_return=("forward_return", "mean"),
            median_forward_return=("forward_return", "median"),
            positive_hit_rate=("forward_return", lambda values: float((values > 0).mean())),
            benchmark_evaluated_count=("benchmark_forward_return", "count"),
            mean_benchmark_return=("benchmark_forward_return", "mean"),
            mean_excess_return_vs_benchmark=("excess_return_vs_benchmark", "mean"),
            median_excess_return_vs_benchmark=("excess_return_vs_benchmark", "median"),
            excess_hit_rate_vs_benchmark=("excess_return_vs_benchmark", lambda values: float((values.dropna() > 0).mean()) if len(values.dropna()) else None),
            sector_proxy_evaluated_count=("sector_proxy_forward_return", "count"),
            mean_sector_proxy_return=("sector_proxy_forward_return", "mean"),
            mean_excess_return_vs_sector_proxy=("excess_return_vs_sector_proxy", "mean"),
            median_excess_return_vs_sector_proxy=("excess_return_vs_sector_proxy", "median"),
            excess_hit_rate_vs_sector_proxy=("excess_return_vs_sector_proxy", lambda values: float((values.dropna() > 0).mean()) if len(values.dropna()) else None),
        )
        .reset_index()
    )
    for column in [
        "mean_forward_return",
        "median_forward_return",
        "positive_hit_rate",
        "mean_benchmark_return",
        "mean_excess_return_vs_benchmark",
        "median_excess_return_vs_benchmark",
        "excess_hit_rate_vs_benchmark",
        "mean_sector_proxy_return",
        "mean_excess_return_vs_sector_proxy",
        "median_excess_return_vs_sector_proxy",
        "excess_hit_rate_vs_sector_proxy",
    ]:
        summary[column] = pd.to_numeric(summary[column], errors="coerce").round(6)
    return {
        "status": "evaluated",
        "note": "Forward returns use the first trading day after the evidence timestamp as the anchor.",
        "rows": frame.to_dict(orient="records"),
        "summary": summary.to_dict(orient="records"),
        "evaluated_rows": int(len(evaluated.dropna(subset=["forward_return"]))),
    }


def load_hypotheses(*, include_inactive: bool = True) -> pd.DataFrame:
    ensure_tables()
    clauses = ["1 = 1"]
    if not include_inactive:
        statuses = "', '".join(sorted(ACTIVE_SCAN_STATUSES))
        clauses.append(f"status IN ('{statuses}')")
    return sql_to_df(
        f"""
        SELECT *
        FROM {HYPOTHESES_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY updated_at DESC NULLS LAST, title
        """
    )


def load_source_events(*, from_date: pd.Timestamp, to_date: pd.Timestamp, sources: list[str] | None = None) -> pd.DataFrame:
    requested = {str(source).lower() for source in (sources or ["news", "announcements"])}
    frames: list[pd.DataFrame] = []
    if "news" in requested and table_exists("advisory_news_events"):
        frames.append(
            sql_to_df(
                """
                SELECT
                    'news' AS source_type,
                    'advisory_news_events' AS source_table,
                    unique_id AS source_key,
                    published_on,
                    symbol,
                    subject,
                    concise_summary_text,
                    source_url
                FROM advisory_news_events
                WHERE published_on >= %s AND published_on <= %s
                """,
                params=(from_date, to_date),
            )
        )
    if "announcements" in requested and table_exists("advisory_watch_events"):
        frames.append(
            sql_to_df(
                """
                SELECT
                    'announcement' AS source_type,
                    'advisory_watch_events' AS source_table,
                    unique_id AS source_key,
                    published_on,
                    symbol,
                    subject,
                    concise_summary_text,
                    NULL::TEXT AS source_url
                FROM advisory_watch_events
                WHERE published_on >= %s AND published_on <= %s
                """,
                params=(from_date, to_date),
            )
        )
    if "announcement_documents" in requested and table_exists("announcement_pipeline_documents"):
        frames.append(
            sql_to_df(
                """
                SELECT
                    'announcement_document' AS source_type,
                    'announcement_pipeline_documents' AS source_table,
                    unique_id AS source_key,
                    published_on,
                    ticker AS symbol,
                    subject,
                    concise_summary_text,
                    attachment_url AS source_url
                FROM announcement_pipeline_documents
                WHERE published_on >= %s AND published_on <= %s
                """,
                params=(from_date, to_date),
            )
        )
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    out["published_on"] = pd.to_datetime(out["published_on"], utc=True, errors="coerce")
    out["symbol"] = out["symbol"].astype("string").str.upper()
    return out


def decision_for_match(hypothesis: pd.Series, matched_terms: list[str], row: pd.Series) -> tuple[str, str, dict[str, Any]]:
    expected_effect = parse_jsonish(hypothesis.get("expected_effect_json"), {})
    effect_text = normalize_text(" ".join(str(value) for value in expected_effect.values())) if isinstance(expected_effect, dict) else normalize_text(expected_effect)
    raw_status = str(hypothesis.get("status") or ACTIVE_REVIEW_STATUS).lower()
    status = normalize_playbook_status(raw_status)
    if "go cash" in effect_text or "cash" in effect_text or "reduce exposure" in effect_text or "market down" in effect_text:
        action = "REDUCE_EXPOSURE_REVIEW"
    elif "short" in effect_text:
        action = "SHORT_RESEARCH_ONLY"
    elif "sector" in effect_text:
        action = "SECTOR_REVIEW"
    else:
        action = "MANUAL_REVIEW"
    if status not in {ACTIVE_REVIEW_STATUS, TRUSTED_OVERLAY_STATUS}:
        action = f"{action}_TESTING"
    reason = f"Matched {len(matched_terms)} trigger term(s): {', '.join(matched_terms[:6])}."
    decision = {
        "hypothesis_status": status,
        "effect": expected_effect,
        "source_type": row.get("source_type"),
        "production_allowed": is_trusted_overlay_status(status),
        "overlay_allowed": is_trusted_overlay_status(status),
        "note": "V1 does not auto-trade hypotheses; it creates an operator decision candidate.",
    }
    return action, reason, decision


def apply_market_context_to_plan(plan: PlaybookActionPlan, market_context: dict[str, Any]) -> tuple[PlaybookActionPlan, dict[str, Any]]:
    adjustment = market_context_adjustment(plan.action_type, market_context)
    update: dict[str, Any] = {}
    if adjustment["adjusted_action_type"] != str(plan.action_type or "").upper():
        update["action_type"] = adjustment["adjusted_action_type"]
    if adjustment.get("urgency_override"):
        update["urgency"] = adjustment["urgency_override"]
    if adjustment.get("production_allowed_override") is not None:
        update["production_allowed"] = bool(adjustment["production_allowed_override"])
    confidence_delta = float(adjustment.get("confidence_delta") or 0.0)
    if confidence_delta:
        update["confidence"] = max(0.0, min(1.0, float(plan.confidence) + confidence_delta))
    risk_controls = dict(plan.risk_controls or {})
    risk_controls["market_context_adjustment"] = adjustment
    risk_controls["market_context"] = market_context
    update["risk_controls"] = risk_controls
    if adjustment["adjustment"] != "none":
        update["decision_reason"] = f"{plan.decision_reason} Market context adjustment: {adjustment['reason']}"
        update["operator_summary"] = f"{plan.operator_summary} Market context: {adjustment['reason']}"
    checks = list(plan.checks or [])
    checks.append(
        PlaybookCheck(
            check_type="market_context",
            question="Does current top-50% market context support, downgrade, or reinforce this playbook action?",
            data_sources=["advisory_market_context_summary_daily", "advisory_market_context_universe_daily"],
            blocking=adjustment["adjustment"] == "positive_event_downgraded_by_market_context",
            rationale=adjustment["reason"],
        )
    )
    update["checks"] = checks
    return plan.model_copy(update=update), adjustment


def _fallback_action_plan(match: pd.Series, *, market_context: dict[str, Any] | None = None, llm_status: str = "fallback", llm_error: str | None = None) -> tuple[PlaybookActionPlan, str, str | None, dict[str, Any]]:
    suggested_action = str(match.get("suggested_action") or "MANUAL_REVIEW")
    market_context = market_context or load_playbook_market_context(match)
    expected_effect = parse_jsonish(match.get("expected_effect_json"), {})
    status = normalize_playbook_status(match.get("status"))
    production_allowed = is_trusted_overlay_status(status)
    urgency = "normal"
    if any(token in suggested_action for token in ["GO_CASH", "REDUCE_EXPOSURE", "SHORT"]):
        urgency = "high"
    if "TESTING" in suggested_action:
        production_allowed = False
    symbol = str(match.get("symbol") or "").strip().upper()
    scope = "symbol" if symbol else "market"
    checks = [
        PlaybookCheck(
            check_type="contradiction",
            question="Is there a credible contradictory source that weakens this playbook trigger?",
            data_sources=["advisory_news_events", "advisory_watch_events", "announcement_pipeline_documents"],
            blocking=True,
            rationale="Investor playbooks still need stale/contradictory evidence checks before action.",
        ),
        PlaybookCheck(
            check_type="price",
            question="Has price already moved enough that the action is late or already priced in?",
            data_sources=["dhan_ohlcv_daily", "dhan_ohlcv_intraday", "advisory_technical_daily"],
            blocking=False,
            rationale="The action should account for realized market reaction after the evidence timestamp.",
        ),
        PlaybookCheck(
            check_type="portfolio",
            question="Which current positions or watchlist names are directly exposed to this trigger?",
            data_sources=["advisory_action_recommendations", "advisory_portfolio_orders", "advisory_watchlist"],
            blocking=False,
            rationale="Playbook actions should be mapped to actual exposure, not treated as abstract headlines.",
        ),
    ]
    if scope == "symbol":
        checks.append(
            PlaybookCheck(
                check_type="event",
                question=f"Does the evidence specifically affect {symbol}, or is it only broad context?",
                data_sources=["advisory_event_evaluations", "advisory_adversarial_reviews"],
                blocking=True,
                rationale="Symbol-level action needs symbol-level causal relevance.",
            )
        )
    plan = PlaybookActionPlan(
        action_type=suggested_action.replace("_TESTING", ""),
        urgency=urgency,
        confidence=float(match.get("match_score") or 0.5),
        production_allowed=production_allowed,
        operator_summary=f"Investor playbook matched {scope} evidence. Suggested action: {suggested_action}.",
        decision_reason=str(match.get("action_reason") or "Matched investor playbook evidence."),
        checks=checks,
        risk_controls={
            "max_action": "review_only" if not production_allowed else suggested_action.replace("_TESTING", ""),
            "requires_operator_approval": True,
            "expected_effect": expected_effect,
        },
        follow_up_window_days=3,
    )
    plan, adjustment = apply_market_context_to_plan(plan, market_context)
    return plan, llm_status, llm_error, adjustment


def _build_action_prompt(match: pd.Series, *, market_context: dict[str, Any]) -> str:
    payload = {
        "playbook_id": match.get("hypothesis_id"),
        "playbook_title": match.get("hypothesis_title"),
        "playbook_status": match.get("status"),
        "source_type": match.get("source_type"),
        "source_table": match.get("source_table"),
        "source_key": match.get("source_key"),
        "published_on": str(match.get("published_on") or ""),
        "symbol": match.get("symbol"),
        "subject": match.get("subject"),
        "evidence_text": match.get("evidence_text"),
        "matched_terms": parse_jsonish(match.get("matched_terms_json"), []),
        "match_score": match.get("match_score"),
        "expected_effect": parse_jsonish(match.get("expected_effect_json"), {}),
        "suggested_action": match.get("suggested_action"),
        "action_reason": match.get("action_reason"),
        "market_context": market_context,
    }
    return (
        "An investor playbook matched a market/news/announcement event. "
        "Create an action plan for the operator. Do not make a trade directly. "
        "Figure out what else should be checked, which data sources should be inspected, "
        "whether action is urgent, and what safe action class should be considered. "
        "Review-overlay action is allowed only when playbook_status is trusted_overlay; otherwise keep it research/review-only.\n\n"
        f"Matched playbook evidence:\n{json.dumps(payload, indent=2, ensure_ascii=False, default=str)}"
    )


def action_plan_for_match(match: pd.Series, *, model: str | None = None, use_llm: bool = True) -> tuple[PlaybookActionPlan, str, str | None, dict[str, Any], dict[str, Any]]:
    market_context = load_playbook_market_context(match)
    if not use_llm:
        plan, llm_status, llm_error, adjustment = _fallback_action_plan(match, market_context=market_context, llm_status="disabled")
        return plan, llm_status, llm_error, adjustment, market_context
    effective_model = model or DEFAULT_PLAYBOOK_ACTION_MODEL
    if effective_model.lower() in {"off", "none", "disabled", "false"}:
        plan, llm_status, llm_error, adjustment = _fallback_action_plan(match, market_context=market_context, llm_status="disabled")
        return plan, llm_status, llm_error, adjustment, market_context
    try:
        codex_model = None if effective_model == "codex" else effective_model.split(":", 1)[1] if effective_model.startswith("codex:") else effective_model
        plan = run_codex_structured(
            _build_action_prompt(match, market_context=market_context),
            response_model=PlaybookActionPlan,
            model=codex_model,
            system_prompt=(
                "You are an investment-operations analyst. "
                "You convert investor playbooks into cautious operator action plans. "
                "You never submit trades. You must identify missing checks and safe action boundaries."
            ),
            max_attempts=2,
        )
        plan, adjustment = apply_market_context_to_plan(plan, market_context)
        return plan, "ok", None, adjustment, market_context
    except Exception as exc:
        plan, llm_status, llm_error, adjustment = _fallback_action_plan(match, market_context=market_context, llm_status="fallback_after_error", llm_error=f"{type(exc).__name__}: {exc}")
        record_fallback_event(
            module="advisory.hypothesis_engine",
            source="playbook_action_plan",
            fallback_type="llm_deterministic_fallback",
            severity="warn",
            symbol=match.get("symbol"),
            unique_id=match.get("source_key"),
            reason="Playbook action-plan LLM failed; deterministic action plan was used.",
            deterministic_fallback=True,
            error=exc,
            metadata={
                "model": effective_model,
                "hypothesis_id": match.get("hypothesis_id"),
                "source_table": match.get("source_table"),
            },
        )
        return plan, llm_status, llm_error, adjustment, market_context


def build_action_plans(matches: pd.DataFrame, *, model: str | None = None, use_llm: bool = True) -> pd.DataFrame:
    if matches.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    now = pd.Timestamp.utcnow()
    for _, match in matches.iterrows():
        plan, llm_status, llm_error, adjustment, market_context = action_plan_for_match(match, model=model, use_llm=use_llm)
        rows.append(
            {
                "planned_at": now,
                "hypothesis_id": match.get("hypothesis_id"),
                "source_table": match.get("source_table"),
                "source_key": match.get("source_key"),
                "symbol": match.get("symbol"),
                "trigger_scope": "symbol" if str(match.get("symbol") or "").strip() else "market",
                "suggested_action": match.get("suggested_action"),
                "action_type": plan.action_type,
                "urgency": plan.urgency,
                "confidence": plan.confidence,
                "production_allowed": bool(plan.production_allowed),
                "operator_summary": plan.operator_summary,
                "decision_reason": plan.decision_reason,
                "checks_json": json_dumps([check.model_dump() for check in plan.checks]),
                "risk_controls_json": json_dumps(plan.risk_controls),
                "action_plan_json": plan.model_dump_json(),
                "market_context_json": json_dumps(market_context),
                "market_context_adjustment_json": json_dumps(adjustment),
                "market_context_adjustment": adjustment.get("adjustment"),
                "prompt_id": PLAYBOOK_ACTION_PROMPT_ID,
                "prompt_version": PLAYBOOK_ACTION_PROMPT_VERSION,
                "prompt_schema_version": PLAYBOOK_ACTION_PROMPT_SCHEMA_VERSION,
                "llm_model": model or DEFAULT_PLAYBOOK_ACTION_MODEL,
                "llm_status": llm_status,
                "llm_error": llm_error,
                "load_ts": now,
            }
        )
    return pd.DataFrame(rows)


def persist_action_plans(plans: pd.DataFrame) -> None:
    ensure_tables()
    if plans.empty:
        return
    out = plans.copy()
    for column in ["planned_at", "load_ts"]:
        out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    out["confidence"] = pd.to_numeric(out["confidence"], errors="coerce")
    out["production_allowed"] = out["production_allowed"].fillna(False).astype(bool)
    upsert_to_db(out, ACTION_PLANS_TABLE, unique_keys=["hypothesis_id", "source_table", "source_key"])


def build_matches(hypotheses: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    if hypotheses.empty or events.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    now = pd.Timestamp.utcnow()
    for _, hypothesis in hypotheses.iterrows():
        trigger_patterns = parse_jsonish(hypothesis.get("trigger_patterns_json"), {})
        terms = normalize_terms(trigger_patterns)
        if not terms:
            terms = normalize_terms(hypothesis.get("description"))
        if not terms:
            continue
        min_terms = int(parse_jsonish(hypothesis.get("decision_policy_json"), {}).get("min_terms", 1) or 1)
        for _, event in events.iterrows():
            evidence_text = " ".join(
                str(event.get(column) or "")
                for column in ["subject", "concise_summary_text"]
            ).strip()
            normalized_evidence = normalize_text(evidence_text)
            matched_terms = [term for term in terms if term in normalized_evidence]
            if len(matched_terms) < min_terms:
                continue
            score = round(min(1.0, len(matched_terms) / max(len(terms), 1)), 4)
            action, reason, decision = decision_for_match(hypothesis, matched_terms, event)
            rows.append(
                {
                    "matched_at": now,
                    "hypothesis_id": hypothesis.get("hypothesis_id"),
                    "hypothesis_title": hypothesis.get("title"),
                    "status": hypothesis.get("status"),
                    "source_type": event.get("source_type"),
                    "source_table": event.get("source_table"),
                    "source_key": event.get("source_key"),
                    "published_on": event.get("published_on"),
                    "symbol": event.get("symbol"),
                    "subject": event.get("subject"),
                    "source_url": event.get("source_url"),
                    "match_score": score,
                    "matched_terms_json": json_dumps(matched_terms),
                    "evidence_text": evidence_text[:4000],
                    "expected_effect_json": hypothesis.get("expected_effect_json"),
                    "suggested_action": action,
                    "action_reason": reason,
                    "decision_json": json_dumps(decision),
                    "load_ts": now,
                }
            )
    return pd.DataFrame(rows)


def persist_matches(matches: pd.DataFrame) -> None:
    ensure_tables()
    if matches.empty:
        return
    out = matches.copy()
    for column in ["matched_at", "published_on", "load_ts"]:
        out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    out["match_score"] = pd.to_numeric(out["match_score"], errors="coerce")
    upsert_to_db(out, MATCHES_TABLE, unique_keys=["hypothesis_id", "source_table", "source_key"])


def run_hypothesis_scan(
    *,
    hypothesis_id: str | None = None,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    sources: list[str] | None = None,
    persist: bool = True,
    build_actions: bool = True,
    use_llm: bool = True,
    model: str | None = None,
) -> dict[str, Any]:
    ensure_tables()
    effective_to = pd.to_datetime(to_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    effective_from = pd.to_datetime(from_date or (effective_to - pd.Timedelta(days=DEFAULT_LOOKBACK_DAYS)), utc=True, errors="coerce")
    hypotheses = load_hypotheses(include_inactive=False)
    if hypothesis_id:
        hypotheses = hypotheses[hypotheses["hypothesis_id"].astype(str) == str(hypothesis_id)]
    events = load_source_events(from_date=effective_from, to_date=effective_to, sources=sources)
    matches = build_matches(hypotheses, events)
    if persist:
        persist_matches(matches)
    action_plans = build_action_plans(matches, model=model, use_llm=use_llm) if build_actions else pd.DataFrame()
    if persist:
        persist_action_plans(action_plans)
        if not action_plans.empty:
            try:
                from advisory.wait_signals import generate_wait_signals

                generate_wait_signals(hypothesis_id=hypothesis_id, limit=max(len(action_plans), 1), persist=True)
            except Exception:
                pass
    return {
        "status": "ok",
        "hypothesis_count": int(len(hypotheses)),
        "source_event_count": int(len(events)),
        "match_count": int(len(matches)),
        "action_plan_count": int(len(action_plans)),
        "matches": matches.head(100).to_dict(orient="records") if not matches.empty else [],
        "action_plans": action_plans.head(100).to_dict(orient="records") if not action_plans.empty else [],
    }


def load_matches(*, hypothesis_id: str | None = None, limit: int = 100) -> pd.DataFrame:
    ensure_tables()
    clauses = ["1 = 1"]
    params: list[Any] = []
    if hypothesis_id:
        clauses.append("hypothesis_id = %s")
        params.append(hypothesis_id)
    return sql_to_df(
        f"""
        SELECT *
        FROM {MATCHES_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC NULLS LAST, matched_at DESC
        LIMIT %s
        """,
        params=tuple([*params, int(limit)]),
    )


def load_action_plans(*, hypothesis_id: str | None = None, limit: int = 100) -> pd.DataFrame:
    ensure_tables()
    clauses = ["1 = 1"]
    params: list[Any] = []
    if hypothesis_id:
        clauses.append("hypothesis_id = %s")
        params.append(hypothesis_id)
    return sql_to_df(
        f"""
        SELECT *
        FROM {ACTION_PLANS_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY planned_at DESC
        LIMIT %s
        """,
        params=tuple([*params, int(limit)]),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create investor playbooks, scan news/announcements, and build action plans.")
    parser.add_argument("--run-scan", action="store_true")
    parser.add_argument("--import-config", nargs="?", const=str(DEFAULT_HYPOTHESES_CONFIG), help="Import versioned playbooks from YAML config.")
    parser.add_argument("--hypothesis-id")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--sources", nargs="*", default=["news", "announcements", "announcement_documents"])
    parser.add_argument("--skip-actions", action="store_true", help="Only persist playbook matches; do not build action plans.")
    parser.add_argument("--no-llm", action="store_true", help="Use deterministic action-plan fallback instead of Codex.")
    parser.add_argument("--model", default=DEFAULT_PLAYBOOK_ACTION_MODEL, help="Action-plan model, e.g. codex, codex:gpt-5.4-mini, or off.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.import_config:
        result = import_hypotheses_config(args.import_config, dry_run=bool(args.dry_run))
    elif args.run_scan:
        result = run_hypothesis_scan(
            hypothesis_id=args.hypothesis_id,
            from_date=pd.Timestamp(args.from_date, tz="UTC") if args.from_date else None,
            to_date=pd.Timestamp(args.to_date, tz="UTC") if args.to_date else None,
            sources=args.sources,
            persist=not bool(args.dry_run),
            build_actions=not bool(args.skip_actions),
            use_llm=not bool(args.no_llm),
            model=args.model,
        )
    else:
        ensure_tables()
        result = {
            "status": "ok",
            "hypotheses_table": HYPOTHESES_TABLE,
            "matches_table": MATCHES_TABLE,
            "action_plans_table": ACTION_PLANS_TABLE,
            "hypotheses": load_hypotheses().head(100).to_dict(orient="records"),
            "action_plans": load_action_plans(limit=100).to_dict(orient="records"),
        }
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
