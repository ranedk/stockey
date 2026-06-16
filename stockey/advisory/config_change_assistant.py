from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import yaml

from advisory.event_policy_promotion import DECISIONS_TABLE as EVENT_POLICY_DECISIONS_TABLE
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.setup_registry import load_ts_forecast_review_rules
from advisory.signal_quality_promotion import DECISIONS_TABLE as SIGNAL_QUALITY_DECISIONS_TABLE
from advisory.technical_threshold_promotion import DECISIONS_TABLE as TECHNICAL_DECISIONS_TABLE
from advisory.ts_forecast_promotion import DECISIONS_TABLE as TS_FORECAST_DECISIONS_TABLE
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "advisory_setups.yaml"
PREVIEWS_TABLE = "advisory_config_change_previews"
APPLICATIONS_TABLE = "advisory_config_change_applications"
PREVIEWS_SCHEMA_MIGRATION_ID = "20260611_advisory_config_change_previews_base"
APPLICATIONS_SCHEMA_MIGRATION_ID = "20260612_advisory_config_change_applications_base"
ChangeSource = Literal["technical_threshold", "signal_quality_overlay", "event_policy_review_rule", "ts_forecast_review_rule"]
ApplicationDecision = Literal["approved_to_apply", "marked_applied", "rejected", "needs_more_data"]
PREVIEWS_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {PREVIEWS_TABLE} (
        preview_id TEXT NOT NULL,
        generated_at TIMESTAMPTZ NOT NULL,
        source_type TEXT NOT NULL,
        source_key TEXT NOT NULL,
        config_path TEXT NOT NULL,
        review_status TEXT,
        decision_status TEXT,
        patch_payload_json TEXT,
        unified_diff TEXT,
        rollback_note TEXT,
        safety_checks_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (preview_id)
    )
    """,
]
APPLICATIONS_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {APPLICATIONS_TABLE} (
        application_id TEXT NOT NULL,
        preview_id TEXT NOT NULL,
        decided_at TIMESTAMPTZ NOT NULL,
        application_decision TEXT NOT NULL,
        operator_id TEXT,
        operator_note TEXT,
        source_type TEXT,
        source_key TEXT,
        config_path TEXT,
        verification_status TEXT,
        verification_json TEXT,
        preview_snapshot_json TEXT,
        safety_checks_json TEXT,
        applied_by_system BOOLEAN,
        load_ts TIMESTAMPTZ,
        UNIQUE (application_id)
    )
    """,
]


def application_decision_effect(application_decision: str, *, verification_status: str | None = None) -> dict[str, Any]:
    normalized = str(application_decision or "").strip().lower()
    base = {
        "application_decision": normalized,
        "mutates_config": False,
        "mutates_policy": False,
        "mutates_action_recommendation": False,
        "mutates_portfolio": False,
        "submits_order": False,
        "broker_execution_allowed": False,
        "applied_by_system": False,
        "verification_status": verification_status,
    }
    if normalized == "approved_to_apply":
        base.update(
            {
                "state": "approved_for_manual_application",
                "closes_review": False,
                "next_step": "Operator may manually apply the reviewed patch outside Stockey, then record marked_applied after verifying config.",
                "effect": "Audit decision only; no config, policy, portfolio, action, or broker state changed.",
            }
        )
    elif normalized == "marked_applied":
        base.update(
            {
                "state": "operator_marked_manual_application",
                "closes_review": verification_status == "verified",
                "next_step": (
                    "Rerun the relevant dry-run checks and advisory flow after the manually applied config is verified."
                    if verification_status == "verified"
                    else "Verify the manually applied config before trusting this application record."
                ),
                "effect": "Audit marker only; Stockey did not edit config or promote live policy.",
            }
        )
    elif normalized == "rejected":
        base.update(
            {
                "state": "rejected",
                "closes_review": True,
                "next_step": "No action. Generate a new reviewed preview if the evidence changes.",
                "effect": "Audit decision only; preview should not be applied.",
            }
        )
    elif normalized == "needs_more_data":
        base.update(
            {
                "state": "needs_more_data",
                "closes_review": False,
                "next_step": "Collect more validation evidence before approving or rejecting this preview.",
                "effect": "Audit decision only; preview remains unapplied.",
            }
        )
    else:
        base.update(
            {
                "state": "unknown",
                "closes_review": False,
                "next_step": "Use a supported application decision.",
                "effect": "No trusted effect can be inferred.",
            }
        )
    return {key: value for key, value in base.items() if value is not None}


def application_operator_boundary(application_decision: str, *, verification_status: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow": "config_change_application",
        "read_only_policy_application": True,
        "system_applies_config": False,
        "system_promotes_policy": False,
        "mutates_portfolio": False,
        "submits_order": False,
        "broker_execution_allowed": False,
        "decision_effect": application_decision_effect(
            application_decision,
            verification_status=verification_status,
        ),
        "note": "Config-change application decisions are audit records. Stockey never edits config files or enables broker execution from this workflow.",
    }


def _record_config_change_fallback(
    fallback_type: str,
    *,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.config_change_assistant",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def parse_jsonish(value: Any, default: Any, *, source: str = "config_change_json") -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return default
    except Exception as exc:
        _record_config_change_fallback(
            "config_change_json_missing_check_failed",
            source=source,
            reason="Config-change assistant could not evaluate missingness for stored JSON and continued parsing.",
            error=exc,
            metadata={"source": source, "value_type": type(value).__name__, "default_type": type(default).__name__},
        )
    try:
        return json.loads(str(value))
    except Exception as exc:
        _record_config_change_fallback(
            "config_change_json_parse_failed",
            source=source,
            reason="Config-change assistant could not parse stored JSON; using the existing default fallback.",
            error=exc,
            metadata={"source": source, "payload_length": len(str(value))},
        )
        return default


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=PREVIEWS_SCHEMA_MIGRATION_ID,
        description="Create advisory config-change preview table.",
        statements=PREVIEWS_SCHEMA_STATEMENTS,
        metadata={"tables": [PREVIEWS_TABLE]},
    )
    apply_schema_migration(
        migration_id=APPLICATIONS_SCHEMA_MIGRATION_ID,
        description="Create advisory config-change application audit table.",
        statements=APPLICATIONS_SCHEMA_STATEMENTS,
        metadata={"tables": [APPLICATIONS_TABLE], "authority_scope": "manual_application_audit_only"},
    )


def _yaml_block(key: str, value: Any, *, indent: int = 4) -> list[str]:
    text = yaml.safe_dump({key: value}, sort_keys=True, default_flow_style=False, allow_unicode=True).rstrip()
    return [(" " * indent) + line for line in text.splitlines()]


def _find_setup_block(lines: list[str], setup_id: str) -> tuple[int, int]:
    marker = f"  - setup_id: {str(setup_id).strip()}"
    start = -1
    for idx, line in enumerate(lines):
        if line.rstrip() == marker:
            start = idx
            break
    if start < 0:
        raise ValueError(f"setup_id not found in config: {setup_id}")
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if lines[idx].startswith("  - setup_id: "):
            end = idx
            break
    return start, end


def _child_block_end(lines: list[str], start: int, block_end: int, *, child_indent: int = 4) -> int:
    prefix = " " * child_indent
    for idx in range(start + 1, block_end):
        line = lines[idx]
        if line.strip() and not line.startswith(prefix + " "):
            return idx
    return block_end


def render_technical_threshold_config(config_text: str, *, setup_id: str, technical_thresholds: dict[str, Any]) -> str:
    if not technical_thresholds:
        raise ValueError("technical_thresholds cannot be empty")
    lines = config_text.splitlines()
    start, end = _find_setup_block(lines, setup_id)
    new_block = _yaml_block("technical_thresholds", technical_thresholds, indent=4)
    existing_start = -1
    for idx in range(start + 1, end):
        if lines[idx].startswith("    technical_thresholds:"):
            existing_start = idx
            break
    if existing_start >= 0:
        existing_end = _child_block_end(lines, existing_start, end, child_indent=4)
        updated = lines[:existing_start] + new_block + lines[existing_end:]
        return "\n".join(updated) + "\n"

    insert_after = -1
    for preferred_key in ["score_thresholds", "technical_rules"]:
        for idx in range(start + 1, end):
            if lines[idx].startswith(f"    {preferred_key}:"):
                insert_after = _child_block_end(lines, idx, end, child_indent=4)
                break
        if insert_after >= 0:
            break
    if insert_after < 0:
        insert_after = start + 1
    updated = lines[:insert_after] + new_block + lines[insert_after:]
    return "\n".join(updated) + "\n"


def render_signal_quality_overlay_config(config_text: str, *, rule_suggestion: dict[str, Any]) -> str:
    overlay = str(rule_suggestion.get("signal_quality_overlay") or rule_suggestion.get("overlay") or "").strip()
    if not overlay:
        raise ValueError("signal_quality_overlay is required")
    rule = {
        "overlay": overlay,
        "minimum_horizon_days": rule_suggestion.get("minimum_horizon_days"),
        "minimum_matured_rows": rule_suggestion.get("minimum_matured_rows"),
        "minimum_lift_vs_technical_only": rule_suggestion.get("minimum_lift_vs_technical_only"),
        "authority": rule_suggestion.get("authority") or "review_input_only",
        "broker_execution_allowed": False,
        "status": "disabled_review_candidate",
    }
    rule = {key: value for key, value in rule.items() if value is not None}
    lines = config_text.splitlines()
    block = yaml.safe_dump([rule], sort_keys=True, default_flow_style=False, allow_unicode=True).rstrip().splitlines()
    indented = ["  " + line for line in block]
    for idx, line in enumerate(lines):
        if line.rstrip() == "signal_quality_overlay_rules:":
            insert_at = idx + 1
            while insert_at < len(lines) and (lines[insert_at].startswith("  ") or not lines[insert_at].strip()):
                insert_at += 1
            updated = lines[:insert_at] + indented + lines[insert_at:]
            return "\n".join(updated) + "\n"
    updated = lines + ["", "signal_quality_overlay_rules:"] + indented
    return "\n".join(updated) + "\n"


def render_event_policy_review_rule_config(config_text: str, *, rule_suggestion: dict[str, Any]) -> str:
    group_type = str(rule_suggestion.get("event_policy_group_type") or rule_suggestion.get("group_type") or "").strip()
    group_value = str(rule_suggestion.get("event_policy_group_value") or rule_suggestion.get("group_value") or "").strip()
    if not group_type or not group_value:
        raise ValueError("event_policy_group_type and event_policy_group_value are required")
    rule = {
        "group_type": group_type,
        "group_value": group_value,
        "minimum_horizon_days": rule_suggestion.get("minimum_horizon_days"),
        "minimum_matured_rows": rule_suggestion.get("minimum_matured_rows"),
        "minimum_avg_forward_return_after_cost": rule_suggestion.get("minimum_avg_forward_return_after_cost"),
        "minimum_hit_rate_after_cost": rule_suggestion.get("minimum_hit_rate_after_cost"),
        "authority": rule_suggestion.get("authority") or "review_input_only",
        "broker_execution_allowed": False,
        "status": "disabled_review_candidate",
    }
    rule = {key: value for key, value in rule.items() if value is not None}
    lines = config_text.splitlines()
    block = yaml.safe_dump([rule], sort_keys=True, default_flow_style=False, allow_unicode=True).rstrip().splitlines()
    indented = ["  " + line for line in block]
    for idx, line in enumerate(lines):
        if line.rstrip() == "event_policy_review_rules:":
            insert_at = idx + 1
            while insert_at < len(lines) and (lines[insert_at].startswith("  ") or not lines[insert_at].strip()):
                insert_at += 1
            updated = lines[:insert_at] + indented + lines[insert_at:]
            return "\n".join(updated) + "\n"
    updated = lines + ["", "event_policy_review_rules:"] + indented
    return "\n".join(updated) + "\n"


def render_ts_forecast_review_rule_config(config_text: str, *, rule_suggestion: dict[str, Any]) -> str:
    model_name = str(rule_suggestion.get("ts_forecast_model_name") or rule_suggestion.get("model_name") or "").strip().lower()
    horizon_days = rule_suggestion.get("ts_forecast_horizon_days") or rule_suggestion.get("horizon_days")
    if not model_name or horizon_days is None:
        raise ValueError("ts_forecast_model_name and ts_forecast_horizon_days are required")
    rule = {
        "model_name": model_name,
        "horizon_days": int(horizon_days),
        "minimum_evaluated_trades": rule_suggestion.get("minimum_evaluated_trades"),
        "minimum_win_rate": rule_suggestion.get("minimum_win_rate"),
        "minimum_avg_cost_adjusted_return": rule_suggestion.get("minimum_avg_cost_adjusted_return"),
        "minimum_lift_vs_momentum": rule_suggestion.get("minimum_lift_vs_momentum"),
        "maximum_exit_conflict_rate": rule_suggestion.get("maximum_exit_conflict_rate"),
        "authority": rule_suggestion.get("authority") or "review_input_only",
        "broker_execution_allowed": False,
        "status": "disabled_review_candidate",
    }
    rule = {key: value for key, value in rule.items() if value is not None}
    lines = config_text.splitlines()
    block = yaml.safe_dump([rule], sort_keys=True, default_flow_style=False, allow_unicode=True).rstrip().splitlines()
    indented = ["  " + line for line in block]
    for idx, line in enumerate(lines):
        if line.rstrip() == "ts_forecast_review_rules:":
            insert_at = idx + 1
            while insert_at < len(lines) and (lines[insert_at].startswith("  ") or not lines[insert_at].strip()):
                insert_at += 1
            updated = lines[:insert_at] + indented + lines[insert_at:]
            return "\n".join(updated) + "\n"
    updated = lines + ["", "ts_forecast_review_rules:"] + indented
    return "\n".join(updated) + "\n"


def build_unified_diff(original_text: str, updated_text: str, *, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            updated_text.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def build_preview_from_patch(
    *,
    source_type: ChangeSource,
    source_key: str,
    patch_payload: dict[str, Any],
    config_path: Path = DEFAULT_CONFIG_PATH,
    persist: bool = True,
) -> dict[str, Any]:
    config_text = config_path.read_text(encoding="utf-8")
    relative_path = str(config_path.relative_to(REPO_ROOT)) if config_path.is_relative_to(REPO_ROOT) else str(config_path)
    if source_type == "technical_threshold":
        updated_text = render_technical_threshold_config(
            config_text,
            setup_id=str(patch_payload.get("setup_id") or ""),
            technical_thresholds=patch_payload.get("technical_thresholds") if isinstance(patch_payload.get("technical_thresholds"), dict) else {},
        )
    elif source_type == "signal_quality_overlay":
        suggestion = patch_payload.get("rule_suggestion") if isinstance(patch_payload.get("rule_suggestion"), dict) else {}
        updated_text = render_signal_quality_overlay_config(config_text, rule_suggestion=suggestion)
    elif source_type == "event_policy_review_rule":
        suggestion = patch_payload.get("rule_suggestion") if isinstance(patch_payload.get("rule_suggestion"), dict) else {}
        updated_text = render_event_policy_review_rule_config(config_text, rule_suggestion=suggestion)
    elif source_type == "ts_forecast_review_rule":
        suggestion = patch_payload.get("rule_suggestion") if isinstance(patch_payload.get("rule_suggestion"), dict) else {}
        updated_text = render_ts_forecast_review_rule_config(config_text, rule_suggestion=suggestion)
    else:
        raise ValueError(f"Unsupported source_type: {source_type}")
    diff = build_unified_diff(config_text, updated_text, path=relative_path)
    if not diff.strip():
        raise ValueError("Generated config diff is empty")
    generated_at = pd.Timestamp.utcnow()
    preview_id = f"{source_type}:{source_key}:{generated_at.isoformat()}"
    result = {
        "status": "ok",
        "preview_id": preview_id,
        "generated_at": generated_at,
        "source_type": source_type,
        "source_key": source_key,
        "config_path": relative_path,
        "review_status": "manual_review_required",
        "decision_status": "preview_only_not_applied",
        "patch_payload": patch_payload,
        "unified_diff": diff,
        "rollback_note": f"Rollback by reverting the reviewed change in {relative_path}; this preview did not modify files.",
        "safety_checks": [
            "Preview only; no file write was performed.",
            "Broker execution remains disabled by this preview.",
            "Apply manually only after checking sample size, trust gate status, and production impact.",
        ],
        "applied": False,
    }
    if persist:
        persist_preview(result)
    return result


def persist_preview(result: dict[str, Any]) -> None:
    ensure_tables()
    row = pd.DataFrame(
        [
            {
                "preview_id": result["preview_id"],
                "generated_at": result["generated_at"],
                "source_type": result["source_type"],
                "source_key": result["source_key"],
                "config_path": result["config_path"],
                "review_status": result["review_status"],
                "decision_status": result["decision_status"],
                "patch_payload_json": json_dumps(result["patch_payload"]),
                "unified_diff": result["unified_diff"],
                "rollback_note": result["rollback_note"],
                "safety_checks_json": json_dumps(result["safety_checks"]),
                "load_ts": pd.Timestamp.utcnow(),
            }
        ]
    )
    upsert_to_db(row, PREVIEWS_TABLE, unique_keys=["preview_id"])


def load_preview(preview_id: str) -> dict[str, Any]:
    ensure_tables()
    normalized_preview_id = str(preview_id or "").strip()
    if not normalized_preview_id:
        raise ValueError("preview_id is required")
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {PREVIEWS_TABLE}
            WHERE preview_id = %(preview_id)s
            LIMIT 1
            """,
            params={"preview_id": normalized_preview_id},
            retries=3,
        )
    except Exception as exc:
        _record_config_change_fallback(
            "config_change_preview_load_failed",
            source=PREVIEWS_TABLE,
            reason="Config-change application audit could not load the requested preview; application recording failed closed.",
            error=exc,
            metadata={"preview_id": normalized_preview_id},
        )
        raise
    if df.empty:
        raise ValueError(f"No config-change preview found for {normalized_preview_id}")
    row = df.iloc[0].to_dict()
    row["patch_payload"] = parse_jsonish(row.get("patch_payload_json"), {})
    row["safety_checks"] = parse_jsonish(row.get("safety_checks_json"), [])
    return row


def verify_preview_against_config(preview: dict[str, Any], *, config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    source_type = str(preview.get("source_type") or "").strip()
    patch_payload = preview.get("patch_payload") if isinstance(preview.get("patch_payload"), dict) else {}
    suggestion = patch_payload.get("rule_suggestion") if isinstance(patch_payload.get("rule_suggestion"), dict) else {}
    if source_type != "ts_forecast_review_rule":
        return {
            "status": "not_supported",
            "source_type": source_type,
            "config_path": str(config_path),
            "verified": False,
            "message": "Automatic config verification is currently implemented only for TS forecast review rules.",
        }
    model_name = str(suggestion.get("ts_forecast_model_name") or suggestion.get("model_name") or "").strip().lower()
    horizon_days = suggestion.get("ts_forecast_horizon_days") or suggestion.get("horizon_days")
    if not model_name or horizon_days is None:
        return {
            "status": "invalid_preview_payload",
            "source_type": source_type,
            "config_path": str(config_path),
            "verified": False,
            "message": "TS forecast preview payload is missing model_name or horizon_days.",
        }
    rules_payload = load_ts_forecast_review_rules(str(config_path))
    matches = [
        rule
        for rule in rules_payload.get("rules", [])
        if str(rule.get("model_name") or "").strip().lower() == model_name and int(rule.get("horizon_days") or 0) == int(horizon_days)
    ]
    valid_matches = [rule for rule in matches if rule.get("valid")]
    return {
        "status": "verified" if valid_matches else "not_found_or_invalid",
        "source_type": source_type,
        "config_path": str(config_path),
        "verified": bool(valid_matches),
        "model_name": model_name,
        "horizon_days": int(horizon_days),
        "matched_count": len(matches),
        "valid_matched_count": len(valid_matches),
        "matched_rules": matches[:5],
        "config_summary": rules_payload.get("summary") or {},
        "issues": rules_payload.get("issues") or [],
        "message": (
            "Matching valid TS forecast review rule is present in config."
            if valid_matches
            else "Matching TS forecast review rule is missing or invalid in config."
        ),
    }


def record_application_decision(
    *,
    preview_id: str,
    application_decision: ApplicationDecision,
    operator_id: str | None = None,
    operator_note: str | None = None,
    verify_config: bool = True,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    normalized_decision = str(application_decision or "").strip().lower()
    if normalized_decision not in {"approved_to_apply", "marked_applied", "rejected", "needs_more_data"}:
        raise ValueError("application_decision must be approved_to_apply, marked_applied, rejected, or needs_more_data")
    preview = load_preview(preview_id)
    verification = verify_preview_against_config(preview, config_path=config_path) if verify_config else {"status": "not_requested", "verified": False}
    decided_at = pd.Timestamp.utcnow()
    application_id = f"{preview_id}:{normalized_decision}:{decided_at.isoformat()}"
    safety_checks = [
        "Application workflow is audit-only; no config file was modified by Stockey.",
        "Broker execution remains disabled by this application audit.",
        "Policy auto-promotion remains disabled; downstream code must explicitly consume reviewed config in a separate change.",
    ]
    decision_effect = application_decision_effect(normalized_decision, verification_status=verification.get("status"))
    operator_boundary = application_operator_boundary(normalized_decision, verification_status=verification.get("status"))
    result = {
        "status": "ok",
        "application_id": application_id,
        "preview_id": preview_id,
        "decided_at": decided_at,
        "application_decision": normalized_decision,
        "operator_id": operator_id,
        "operator_note": operator_note,
        "source_type": preview.get("source_type"),
        "source_key": preview.get("source_key"),
        "config_path": preview.get("config_path"),
        "verification_status": verification.get("status"),
        "verification": verification,
        "preview": {
            "preview_id": preview.get("preview_id"),
            "source_type": preview.get("source_type"),
            "source_key": preview.get("source_key"),
            "config_path": preview.get("config_path"),
            "decision_status": preview.get("decision_status"),
            "patch_payload": preview.get("patch_payload"),
        },
        "safety_checks": safety_checks,
        "decision_effect": decision_effect,
        "operator_boundary": operator_boundary,
        "applied_by_system": False,
        "applied": False,
        "note": "Decision recorded only. Stockey did not modify config files, action rules, portfolio rows, or broker behavior.",
    }
    row = pd.DataFrame(
        [
            {
                "application_id": application_id,
                "preview_id": preview_id,
                "decided_at": decided_at,
                "application_decision": normalized_decision,
                "operator_id": operator_id,
                "operator_note": operator_note,
                "source_type": preview.get("source_type"),
                "source_key": preview.get("source_key"),
                "config_path": preview.get("config_path"),
                "verification_status": verification.get("status"),
                "verification_json": json_dumps(verification),
                "preview_snapshot_json": json_dumps(result["preview"]),
                "safety_checks_json": json_dumps(safety_checks),
                "applied_by_system": False,
                "load_ts": pd.Timestamp.utcnow(),
            }
        ]
    )
    ensure_tables()
    upsert_to_db(row, APPLICATIONS_TABLE, unique_keys=["application_id"])
    return result


def load_application_decisions(limit: int = 25, *, include_preview_snapshot: bool = False) -> list[dict[str, Any]]:
    ensure_tables()
    select_columns = [
        "application_id",
        "preview_id",
        "decided_at",
        "application_decision",
        "operator_id",
        "operator_note",
        "source_type",
        "source_key",
        "config_path",
        "verification_status",
        "verification_json",
        "safety_checks_json",
        "applied_by_system",
        "load_ts",
    ]
    if include_preview_snapshot:
        select_columns.append("preview_snapshot_json")
    try:
        df = sql_to_df(
            f"""
            SELECT {", ".join(select_columns)}
            FROM {APPLICATIONS_TABLE}
            ORDER BY decided_at DESC
            LIMIT %(limit)s
            """,
            params={"limit": max(1, int(limit))},
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_config_change_fallback(
            "config_change_applications_load_failed",
            source=APPLICATIONS_TABLE,
            reason="Config-change application audit list could not be loaded; operator application history may be unavailable.",
            error=exc,
            metadata={"limit": int(limit), "include_preview_snapshot": bool(include_preview_snapshot)},
        )
        raise
    if df.empty:
        return []
    out = df.copy()
    for col in ["verification_json", "preview_snapshot_json", "safety_checks_json"]:
        if col in out.columns:
            out[col.replace("_json", "")] = out[col].map(lambda value: parse_jsonish(value, [] if col == "safety_checks_json" else {}))
    out = out.astype(object).where(pd.notna(out), None)
    rows = out.to_dict(orient="records")
    for row in rows:
        decision = str(row.get("application_decision") or "").strip().lower()
        verification_status = str(row.get("verification_status") or "").strip() or None
        row["decision_effect"] = application_decision_effect(decision, verification_status=verification_status)
        row["operator_boundary"] = application_operator_boundary(decision, verification_status=verification_status)
    return rows


def load_latest_approved_technical_decision(*, setup_id: str, config_id: str, reviewed_at: Any | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"setup_id": str(setup_id), "config_id": str(config_id)}
    reviewed_filter = ""
    if reviewed_at:
        parsed = pd.to_datetime(reviewed_at, utc=True, errors="coerce")
        if pd.isna(parsed):
            raise ValueError(f"Invalid reviewed_at: {reviewed_at}")
        params["reviewed_at"] = parsed
        reviewed_filter = "AND reviewed_at = %(reviewed_at)s"
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {TECHNICAL_DECISIONS_TABLE}
            WHERE setup_id = %(setup_id)s
              AND config_id = %(config_id)s
              AND decision = 'approved'
              {reviewed_filter}
            ORDER BY decided_at DESC
            LIMIT 1
            """,
            params=params,
            retries=3,
        )
    except Exception as exc:
        _record_config_change_fallback(
            "config_change_technical_decision_load_failed",
            source=TECHNICAL_DECISIONS_TABLE,
            reason="Config-change preview could not load an approved technical-threshold decision; preview generation failed closed.",
            error=exc,
            metadata={"setup_id": setup_id, "config_id": config_id, "reviewed_at": str(reviewed_at) if reviewed_at else None},
        )
        raise
    if df.empty:
        raise ValueError(f"No approved technical threshold decision found for {setup_id}/{config_id}")
    row = df.iloc[0].to_dict()
    row["final_patch"] = parse_jsonish(row.get("final_patch_json"), {})
    return row


def load_latest_approved_signal_quality_decision(
    *,
    evaluated_at: Any,
    horizon_days: int,
    variant: str,
    reviewed_at: Any | None = None,
) -> dict[str, Any]:
    parsed_evaluated = pd.to_datetime(evaluated_at, utc=True, errors="coerce")
    if pd.isna(parsed_evaluated):
        raise ValueError(f"Invalid evaluated_at: {evaluated_at}")
    params: dict[str, Any] = {
        "evaluated_at": parsed_evaluated,
        "horizon_days": int(horizon_days),
        "variant": str(variant),
    }
    reviewed_filter = ""
    if reviewed_at:
        parsed_reviewed = pd.to_datetime(reviewed_at, utc=True, errors="coerce")
        if pd.isna(parsed_reviewed):
            raise ValueError(f"Invalid reviewed_at: {reviewed_at}")
        params["reviewed_at"] = parsed_reviewed
        reviewed_filter = "AND reviewed_at = %(reviewed_at)s"
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {SIGNAL_QUALITY_DECISIONS_TABLE}
            WHERE evaluated_at = %(evaluated_at)s
              AND horizon_days = %(horizon_days)s
              AND variant = %(variant)s
              AND decision = 'approved'
              {reviewed_filter}
            ORDER BY decided_at DESC
            LIMIT 1
            """,
            params=params,
            retries=3,
        )
    except Exception as exc:
        _record_config_change_fallback(
            "config_change_signal_quality_decision_load_failed",
            source=SIGNAL_QUALITY_DECISIONS_TABLE,
            reason="Config-change preview could not load an approved signal-quality overlay decision; preview generation failed closed.",
            error=exc,
            metadata={
                "evaluated_at": str(evaluated_at),
                "horizon_days": int(horizon_days),
                "variant": variant,
                "reviewed_at": str(reviewed_at) if reviewed_at else None,
            },
        )
        raise
    if df.empty:
        raise ValueError(f"No approved signal-quality decision found for {evaluated_at}/{horizon_days}/{variant}")
    row = df.iloc[0].to_dict()
    row["final_patch"] = parse_jsonish(row.get("final_patch_json"), {})
    return row


def load_latest_approved_event_policy_decision(
    *,
    evaluated_at: Any,
    horizon_days: int,
    group_type: str,
    group_value: str,
    reviewed_at: Any | None = None,
) -> dict[str, Any]:
    parsed_evaluated = pd.to_datetime(evaluated_at, utc=True, errors="coerce")
    if pd.isna(parsed_evaluated):
        raise ValueError(f"Invalid evaluated_at: {evaluated_at}")
    normalized_group_type = str(group_type or "").strip()
    normalized_group_value = str(group_value or "").strip()
    if not normalized_group_type or not normalized_group_value:
        raise ValueError("group_type and group_value are required")
    params: dict[str, Any] = {
        "evaluated_at": parsed_evaluated,
        "horizon_days": int(horizon_days),
        "group_type": normalized_group_type,
        "group_value": normalized_group_value,
    }
    reviewed_filter = ""
    if reviewed_at:
        parsed_reviewed = pd.to_datetime(reviewed_at, utc=True, errors="coerce")
        if pd.isna(parsed_reviewed):
            raise ValueError(f"Invalid reviewed_at: {reviewed_at}")
        params["reviewed_at"] = parsed_reviewed
        reviewed_filter = "AND reviewed_at = %(reviewed_at)s"
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {EVENT_POLICY_DECISIONS_TABLE}
            WHERE evaluated_at = %(evaluated_at)s
              AND horizon_days = %(horizon_days)s
              AND group_type = %(group_type)s
              AND group_value = %(group_value)s
              AND decision = 'approved'
              {reviewed_filter}
            ORDER BY decided_at DESC
            LIMIT 1
            """,
            params=params,
            retries=3,
        )
    except Exception as exc:
        _record_config_change_fallback(
            "config_change_event_policy_decision_load_failed",
            source=EVENT_POLICY_DECISIONS_TABLE,
            reason="Config-change preview could not load an approved event-policy review-rule decision; preview generation failed closed.",
            error=exc,
            metadata={
                "evaluated_at": str(evaluated_at),
                "horizon_days": int(horizon_days),
                "group_type": normalized_group_type,
                "group_value": normalized_group_value,
                "reviewed_at": str(reviewed_at) if reviewed_at else None,
            },
        )
        raise
    if df.empty:
        raise ValueError(f"No approved event-policy decision found for {evaluated_at}/{horizon_days}/{group_type}/{group_value}")
    row = df.iloc[0].to_dict()
    row["final_patch"] = parse_jsonish(row.get("final_patch_json"), {})
    return row


def load_latest_approved_ts_forecast_decision(
    *,
    model_name: str,
    horizon_days: int,
    reviewed_at: Any | None = None,
) -> dict[str, Any]:
    normalized_model = str(model_name or "").strip().lower()
    if not normalized_model:
        raise ValueError("model_name is required")
    params: dict[str, Any] = {
        "model_name": normalized_model,
        "horizon_days": int(horizon_days),
    }
    reviewed_filter = ""
    if reviewed_at:
        parsed_reviewed = pd.to_datetime(reviewed_at, utc=True, errors="coerce")
        if pd.isna(parsed_reviewed):
            raise ValueError(f"Invalid reviewed_at: {reviewed_at}")
        params["reviewed_at"] = parsed_reviewed
        reviewed_filter = "AND reviewed_at = %(reviewed_at)s"
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {TS_FORECAST_DECISIONS_TABLE}
            WHERE model_name = %(model_name)s
              AND horizon_days = %(horizon_days)s
              AND decision = 'approved'
              {reviewed_filter}
            ORDER BY decided_at DESC
            LIMIT 1
            """,
            params=params,
            retries=3,
        )
    except Exception as exc:
        _record_config_change_fallback(
            "config_change_ts_forecast_decision_load_failed",
            source=TS_FORECAST_DECISIONS_TABLE,
            reason="Config-change preview could not load an approved TS forecast review-rule decision; preview generation failed closed.",
            error=exc,
            metadata={
                "model_name": normalized_model,
                "horizon_days": int(horizon_days),
                "reviewed_at": str(reviewed_at) if reviewed_at else None,
            },
        )
        raise
    if df.empty:
        raise ValueError(f"No approved TS forecast decision found for {normalized_model}/{horizon_days}")
    row = df.iloc[0].to_dict()
    row["final_patch"] = parse_jsonish(row.get("final_patch_json"), {})
    return row


def build_technical_threshold_preview(*, setup_id: str, config_id: str, reviewed_at: Any | None = None, persist: bool = True) -> dict[str, Any]:
    decision = load_latest_approved_technical_decision(setup_id=setup_id, config_id=config_id, reviewed_at=reviewed_at)
    source_key = f"{decision.get('reviewed_at')}:{setup_id}:{config_id}"
    result = build_preview_from_patch(
        source_type="technical_threshold",
        source_key=source_key,
        patch_payload=decision.get("final_patch") or {},
        persist=persist,
    )
    result["decision"] = {key: decision.get(key) for key in ["decided_at", "reviewed_at", "setup_id", "config_id", "operator_id", "decision_reason"]}
    return result


def build_signal_quality_overlay_preview(
    *,
    evaluated_at: Any,
    horizon_days: int,
    variant: str,
    reviewed_at: Any | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    decision = load_latest_approved_signal_quality_decision(
        evaluated_at=evaluated_at,
        horizon_days=horizon_days,
        variant=variant,
        reviewed_at=reviewed_at,
    )
    source_key = f"{decision.get('reviewed_at')}:{decision.get('evaluated_at')}:{horizon_days}:{variant}"
    result = build_preview_from_patch(
        source_type="signal_quality_overlay",
        source_key=source_key,
        patch_payload=decision.get("final_patch") or {},
        persist=persist,
    )
    result["decision"] = {
        key: decision.get(key)
        for key in ["decided_at", "reviewed_at", "evaluated_at", "horizon_days", "variant", "operator_id", "decision_reason"]
    }
    return result


def build_event_policy_review_rule_preview(
    *,
    evaluated_at: Any,
    horizon_days: int,
    group_type: str,
    group_value: str,
    reviewed_at: Any | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    decision = load_latest_approved_event_policy_decision(
        evaluated_at=evaluated_at,
        horizon_days=horizon_days,
        group_type=group_type,
        group_value=group_value,
        reviewed_at=reviewed_at,
    )
    source_key = f"{decision.get('reviewed_at')}:{decision.get('evaluated_at')}:{horizon_days}:{group_type}:{group_value}"
    result = build_preview_from_patch(
        source_type="event_policy_review_rule",
        source_key=source_key,
        patch_payload=decision.get("final_patch") or {},
        persist=persist,
    )
    result["decision"] = {
        key: decision.get(key)
        for key in ["decided_at", "reviewed_at", "evaluated_at", "horizon_days", "group_type", "group_value", "operator_id", "decision_reason"]
    }
    return result


def build_ts_forecast_review_rule_preview(
    *,
    model_name: str,
    horizon_days: int,
    reviewed_at: Any | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    decision = load_latest_approved_ts_forecast_decision(
        model_name=model_name,
        horizon_days=horizon_days,
        reviewed_at=reviewed_at,
    )
    source_key = f"{decision.get('reviewed_at')}:{decision.get('model_name')}:{horizon_days}"
    result = build_preview_from_patch(
        source_type="ts_forecast_review_rule",
        source_key=source_key,
        patch_payload=decision.get("final_patch") or {},
        persist=persist,
    )
    result["decision"] = {
        key: decision.get(key)
        for key in ["decided_at", "reviewed_at", "model_name", "horizon_days", "operator_id", "decision_reason"]
    }
    return result


def load_previews(limit: int = 25) -> list[dict[str, Any]]:
    ensure_tables()
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {PREVIEWS_TABLE}
            ORDER BY generated_at DESC
            LIMIT %(limit)s
            """,
            params={"limit": max(1, int(limit))},
            retries=3,
        )
    except Exception as exc:
        _record_config_change_fallback(
            "config_change_previews_load_failed",
            source=PREVIEWS_TABLE,
            reason="Config-change preview list could not be loaded; operator preview history may be unavailable.",
            error=exc,
            metadata={"limit": int(limit)},
        )
        raise
    if df.empty:
        return []
    out = df.copy()
    for col in ["patch_payload_json", "safety_checks_json"]:
        if col in out.columns:
            out[col.replace("_json", "")] = out[col].map(lambda value: parse_jsonish(value, [] if col == "safety_checks_json" else {}))
    out = out.astype(object).where(pd.notna(out), None)
    return out.to_dict(orient="records")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate reviewed config diffs from approved promotion decisions without applying them.")
    parser.add_argument("--source-type", choices=["technical_threshold", "signal_quality_overlay", "event_policy_review_rule", "ts_forecast_review_rule"], required=True)
    parser.add_argument("--setup-id")
    parser.add_argument("--config-id")
    parser.add_argument("--evaluated-at")
    parser.add_argument("--horizon-days", type=int)
    parser.add_argument("--variant")
    parser.add_argument("--group-type")
    parser.add_argument("--group-value")
    parser.add_argument("--model-name")
    parser.add_argument("--reviewed-at")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.source_type == "technical_threshold":
        if not args.setup_id or not args.config_id:
            raise SystemExit("--setup-id and --config-id are required for technical_threshold")
        result = build_technical_threshold_preview(
            setup_id=args.setup_id,
            config_id=args.config_id,
            reviewed_at=args.reviewed_at,
            persist=not bool(args.dry_run),
        )
    elif args.source_type == "signal_quality_overlay":
        if not args.evaluated_at or args.horizon_days is None or not args.variant:
            raise SystemExit("--evaluated-at, --horizon-days, and --variant are required for signal_quality_overlay")
        result = build_signal_quality_overlay_preview(
            evaluated_at=args.evaluated_at,
            horizon_days=int(args.horizon_days),
            variant=args.variant,
            reviewed_at=args.reviewed_at,
            persist=not bool(args.dry_run),
        )
    elif args.source_type == "event_policy_review_rule":
        if not args.evaluated_at or args.horizon_days is None or not args.group_type or not args.group_value:
            raise SystemExit("--evaluated-at, --horizon-days, --group-type, and --group-value are required for event_policy_review_rule")
        result = build_event_policy_review_rule_preview(
            evaluated_at=args.evaluated_at,
            horizon_days=int(args.horizon_days),
            group_type=args.group_type,
            group_value=args.group_value,
            reviewed_at=args.reviewed_at,
            persist=not bool(args.dry_run),
        )
    else:
        if not args.model_name or args.horizon_days is None:
            raise SystemExit("--model-name and --horizon-days are required for ts_forecast_review_rule")
        result = build_ts_forecast_review_rule_preview(
            model_name=args.model_name,
            horizon_days=int(args.horizon_days),
            reviewed_at=args.reviewed_at,
            persist=not bool(args.dry_run),
        )
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
