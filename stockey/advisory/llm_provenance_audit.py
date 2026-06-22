from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.prompt_registry import get_prompt_contract
from utils.db import sql_to_df


@dataclass(frozen=True)
class ProvenanceSpec:
    table_name: str
    label: str
    prompt_id: str
    date_column: str
    prompt_id_column: str
    prompt_version_column: str
    prompt_schema_column: str
    model_column: str | None = None
    evidence_columns: tuple[str, ...] = field(default_factory=tuple)
    authority_columns: tuple[str, ...] = field(default_factory=tuple)
    status_column: str | None = None
    exclude_statuses: tuple[str, ...] = field(default_factory=tuple)

    @property
    def audit_columns(self) -> list[str]:
        columns = [
            self.date_column,
            self.prompt_id_column,
            self.prompt_version_column,
            self.prompt_schema_column,
        ]
        if self.model_column:
            columns.append(self.model_column)
        if self.status_column:
            columns.append(self.status_column)
        columns.extend(self.evidence_columns)
        columns.extend(self.authority_columns)
        return list(dict.fromkeys(columns))


PROVENANCE_SPECS: tuple[ProvenanceSpec, ...] = (
    ProvenanceSpec(
        table_name="advisory_event_evaluations",
        label="event_evaluation",
        prompt_id="advisory_event_evaluation",
        date_column="evaluated_at",
        prompt_id_column="prompt_id",
        prompt_version_column="prompt_version",
        prompt_schema_column="prompt_schema_version",
        model_column="model_name",
        evidence_columns=("event_tensor_json", "source_trace_json", "context_snapshot_json"),
        status_column="evaluation_status",
    ),
    ProvenanceSpec(
        table_name="advisory_event_policy_actions",
        label="event_policy_manual_review",
        prompt_id="event_policy_manual_review",
        date_column="policy_at",
        prompt_id_column="llm_prompt_id",
        prompt_version_column="llm_prompt_version",
        prompt_schema_column="llm_prompt_schema_version",
        model_column="llm_review_model",
        evidence_columns=("llm_review_json", "actionability_json", "raw_context_json"),
        authority_columns=("raw_context_json",),
        status_column="llm_review_status",
        exclude_statuses=("not_requested",),
    ),
    ProvenanceSpec(
        table_name="advisory_company_memory_reviews",
        label="company_memory_review",
        prompt_id="company_memory_review",
        date_column="review_date",
        prompt_id_column="prompt_id",
        prompt_version_column="prompt_version",
        prompt_schema_column="prompt_schema_version",
        model_column="model_name",
        evidence_columns=("evidence_used_json", "payload_json"),
        authority_columns=("authority_scope", "deterministic_boundary"),
        status_column="review_status",
    ),
    ProvenanceSpec(
        table_name="advisory_playbook_action_plans",
        label="playbook_action_plan",
        prompt_id="playbook_action_plan",
        date_column="planned_at",
        prompt_id_column="prompt_id",
        prompt_version_column="prompt_version",
        prompt_schema_column="prompt_schema_version",
        model_column="llm_model",
        evidence_columns=("action_plan_json", "checks_json", "market_context_json"),
        authority_columns=("production_allowed",),
        status_column="llm_status",
    ),
    ProvenanceSpec(
        table_name="advisory_action_recommendations",
        label="manual_revision_pointers",
        prompt_id="manual_revision_pointers",
        date_column="load_ts",
        prompt_id_column="manual_revision_prompt_id",
        prompt_version_column="manual_revision_prompt_version",
        prompt_schema_column="manual_revision_prompt_schema_version",
        model_column="manual_revision_model",
        evidence_columns=("manual_revision_pointers_json", "recommendation_reason_json", "raw_context_json"),
        status_column="manual_revision_status",
    ),
)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        if bool(pd.isna(value)):
            return True
    except (TypeError, ValueError) as exc:
        record_local_fallback_event(
            module="advisory.llm_provenance_audit",
            fallback_type="llm_provenance_missing_check_failed",
            source="is_missing",
            severity="warn",
            reason="LLM provenance audit could not evaluate missingness for a value and treated it as present.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    if isinstance(value, str):
        text = value.strip()
        return text == "" or text.lower() in {"nan", "none", "null", "{}", "[]"}
    return False


def _missing_count(series: pd.Series) -> int:
    return int(series.map(_is_missing).sum())


def _blank_series(length: int) -> pd.Series:
    return pd.Series([None] * int(length))


def _column_missing_count(frame: pd.DataFrame, column: str) -> int:
    if column not in frame.columns:
        return int(len(frame))
    return _missing_count(frame[column])


def _missing_any_count(frame: pd.DataFrame, columns: tuple[str, ...]) -> int:
    if not columns:
        return 0
    if frame.empty:
        return 0
    present = [column for column in columns if column in frame.columns]
    if not present:
        return int(len(frame))
    missing = pd.Series([True] * len(frame), index=frame.index)
    for column in present:
        missing &= frame[column].map(_is_missing)
    return int(missing.sum())


def _jsonish(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if _is_missing(value):
        return None
    try:
        text = str(value).strip()
        if not text or text[0] not in "{[":
            return value
        return json.loads(text)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.llm_provenance_audit",
            fallback_type="llm_provenance_authority_json_parse_failed",
            source="authority_scan",
            severity="warn",
            reason="LLM provenance audit could not parse an authority payload while scanning for forbidden authority.",
            error=exc,
            metadata={"value_type": type(value).__name__, "payload_length": len(str(value))},
        )
        return value


def _truthy_authority(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if _is_missing(value):
        return False
    return str(value).strip().lower() in {"true", "t", "1", "yes", "y", "enabled", "allow", "allowed"}


def _portfolio_authority_forbidden(value: Any) -> bool:
    if _is_missing(value):
        return False
    text = str(value).strip().lower()
    return text not in {"", "none", "no", "false", "n/a", "na", "null", "read_only", "review_only"}


def _authority_violations(value: Any, *, path: str = "") -> list[str]:
    value = _jsonish(value)
    violations: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.strip().lower()
            child_path = f"{path}.{key_text}" if path else key_text
            if normalized in {"broker_execution_allowed", "broker_execution_enabled", "submits_order", "submit_broker_order"}:
                if _truthy_authority(item):
                    violations.append(child_path)
                continue
            if normalized in {"policy_auto_promotion_allowed", "auto_promotion_allowed", "production_allowed", "mutates_config"}:
                if _truthy_authority(item):
                    violations.append(child_path)
                continue
            if normalized in {"portfolio_authority", "portfolio_mutation_authority"}:
                if _portfolio_authority_forbidden(item):
                    violations.append(child_path)
                continue
            if normalized in {"mutates_portfolio", "portfolio_mutation_allowed"}:
                if _truthy_authority(item):
                    violations.append(child_path)
                continue
            violations.extend(_authority_violations(item, path=child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            violations.extend(_authority_violations(item, path=f"{path}[{index}]" if path else f"[{index}]"))
    return violations


def _authority_violation_summary(frame: pd.DataFrame, columns: tuple[str, ...]) -> dict[str, Any]:
    if not columns or frame.empty:
        return {"count": 0, "columns": {}, "samples": []}
    by_column: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    row_indexes: set[int] = set()
    for column in columns:
        if column not in frame.columns:
            continue
        normalized_column = str(column).strip().lower()
        for idx, value in frame[column].items():
            violations: list[str] = []
            if normalized_column in {"broker_execution_allowed", "broker_execution_enabled", "submits_order", "submit_broker_order"}:
                if _truthy_authority(value):
                    violations.append(column)
            elif normalized_column in {"policy_auto_promotion_allowed", "auto_promotion_allowed", "production_allowed", "mutates_config"}:
                if _truthy_authority(value):
                    violations.append(column)
            elif normalized_column in {"portfolio_authority", "portfolio_mutation_authority"}:
                if _portfolio_authority_forbidden(value):
                    violations.append(column)
            elif normalized_column in {"mutates_portfolio", "portfolio_mutation_allowed"}:
                if _truthy_authority(value):
                    violations.append(column)
            else:
                violations = _authority_violations(value, path=column)
            if not violations:
                continue
            row_indexes.add(int(idx) if isinstance(idx, int) else len(row_indexes))
            by_column[column] = by_column.get(column, 0) + 1
            if len(samples) < 5:
                samples.append({"row_index": idx, "column": column, "violations": violations[:10]})
    return {"count": len(row_indexes), "columns": by_column, "samples": samples}


def _prompt_mismatch_count(frame: pd.DataFrame, spec: ProvenanceSpec) -> int:
    if frame.empty or spec.prompt_id_column not in frame.columns:
        return 0
    expected = str(spec.prompt_id).strip()
    values = frame[spec.prompt_id_column].fillna("").astype(str).str.strip()
    return int((values != expected).sum())


def audit_frame(spec: ProvenanceSpec, frame: pd.DataFrame, *, missing_columns: list[str] | None = None) -> dict[str, Any]:
    contract = get_prompt_contract(spec.prompt_id)
    missing_columns = list(missing_columns or [])
    row_count = int(len(frame))
    required_metadata = [
        spec.prompt_id_column,
        spec.prompt_version_column,
        spec.prompt_schema_column,
    ]
    if spec.model_column:
        required_metadata.append(spec.model_column)
    missing_counts = {column: _column_missing_count(frame, column) for column in required_metadata if column not in missing_columns}
    evidence_missing_count = _missing_any_count(frame, spec.evidence_columns)
    authority_violation = _authority_violation_summary(frame, spec.authority_columns)
    authority_violation_count = int(authority_violation.get("count") or 0)
    prompt_mismatch_count = _prompt_mismatch_count(frame, spec)
    metadata_issue_count = (
        sum(int(value) for value in missing_counts.values())
        + int(evidence_missing_count)
        + int(prompt_mismatch_count)
        + authority_violation_count
    )
    if missing_columns:
        status = "missing_columns"
    elif authority_violation_count:
        status = "forbidden_authority_detected"
    elif row_count == 0:
        status = "no_recent_rows"
    elif metadata_issue_count:
        status = "rows_missing_required_metadata"
    else:
        status = "ok"
    return {
        "table_name": spec.table_name,
        "label": spec.label,
        "status": status,
        "row_count": row_count,
        "missing_columns": missing_columns,
        "missing_counts": missing_counts,
        "evidence_missing_count": evidence_missing_count,
        "authority_violation_count": authority_violation_count,
        "authority_violation_columns": authority_violation.get("columns") or {},
        "authority_violation_samples": authority_violation.get("samples") or [],
        "prompt_mismatch_count": prompt_mismatch_count,
        "expected_prompt": {
            "prompt_id": spec.prompt_id,
            "prompt_version": contract.get("version"),
            "prompt_schema_version": contract.get("response_schema_version"),
            "authority_scope": contract.get("authority_scope"),
            "broker_execution_allowed": bool(contract.get("broker_execution_allowed")),
        },
        "authority_columns": list(spec.authority_columns),
        "evidence_columns": list(spec.evidence_columns),
        "policy_boundary": {
            "audit_only": True,
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        },
    }


def _table_exists(table_name: str) -> bool:
    df = sql_to_df(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = %s
        ) AS exists
        """,
        params=(table_name,),
    )
    return bool(df.iloc[0]["exists"]) if not df.empty and "exists" in df.columns else False


def _table_columns(table_name: str) -> set[str]:
    df = sql_to_df(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        params=(table_name,),
    )
    if df.empty or "column_name" not in df.columns:
        return set()
    return set(df["column_name"].astype(str).tolist())


def _quote_identifier(identifier: str) -> str:
    safe = str(identifier).replace('"', '""')
    return f'"{safe}"'


def load_recent_rows(spec: ProvenanceSpec, *, lookback_days: int, limit: int) -> tuple[pd.DataFrame, list[str]]:
    if not _table_exists(spec.table_name):
        return pd.DataFrame(), ["__table_missing__"]
    available_columns = _table_columns(spec.table_name)
    required_for_query = [spec.date_column, spec.prompt_id_column, spec.prompt_version_column, spec.prompt_schema_column]
    missing_columns = [column for column in required_for_query if column not in available_columns]
    selected = [column for column in spec.audit_columns if column in available_columns]
    if not selected:
        return pd.DataFrame(), missing_columns or list(spec.audit_columns)
    date_filter = ""
    params: list[Any] = []
    if spec.date_column in available_columns and lookback_days > 0:
        date_filter = f"WHERE {_quote_identifier(spec.date_column)} >= (NOW() - (%s::TEXT || ' days')::INTERVAL)"
        params.append(int(lookback_days))
    if spec.status_column and spec.exclude_statuses and spec.status_column in available_columns:
        status_filter = (
            f"COALESCE(LOWER(TRIM({_quote_identifier(spec.status_column)}::TEXT)), '') "
            f"<> ALL(%s::TEXT[])"
        )
        date_filter = f"{date_filter} AND {status_filter}" if date_filter else f"WHERE {status_filter}"
        params.append([str(value).strip().lower() for value in spec.exclude_statuses])
    order_column = spec.date_column if spec.date_column in available_columns else selected[0]
    params.append(int(limit))
    columns_sql = ", ".join(_quote_identifier(column) for column in selected)
    query = f"""
        SELECT {columns_sql}
        FROM {_quote_identifier(spec.table_name)}
        {date_filter}
        ORDER BY {_quote_identifier(order_column)} DESC NULLS LAST
        LIMIT %s
    """
    return sql_to_df(query, params=tuple(params)), missing_columns


def build_llm_provenance_audit(*, lookback_days: int = 30, limit_per_table: int = 500) -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    for spec in PROVENANCE_SPECS:
        try:
            frame, missing_columns = load_recent_rows(spec, lookback_days=lookback_days, limit=limit_per_table)
            if "__table_missing__" in missing_columns:
                contract = get_prompt_contract(spec.prompt_id)
                tables.append(
                    {
                        "table_name": spec.table_name,
                        "label": spec.label,
                        "status": "missing_table",
                        "row_count": 0,
                        "missing_columns": [],
                        "missing_counts": {},
                        "evidence_missing_count": 0,
                        "prompt_mismatch_count": 0,
                        "expected_prompt": {
                            "prompt_id": spec.prompt_id,
                            "prompt_version": contract.get("version"),
                            "prompt_schema_version": contract.get("response_schema_version"),
                            "authority_scope": contract.get("authority_scope"),
                            "broker_execution_allowed": bool(contract.get("broker_execution_allowed")),
                        },
                        "policy_boundary": {
                            "audit_only": True,
                            "broker_execution_allowed": False,
                            "policy_auto_promotion_allowed": False,
                        },
                    }
                )
                continue
            tables.append(audit_frame(spec, frame, missing_columns=missing_columns))
        except Exception as exc:
            record_local_fallback_event(
                module="advisory.llm_provenance_audit",
                fallback_type="llm_provenance_table_audit_failed",
                source=spec.table_name,
                severity="warn",
                reason="LLM provenance audit could not load or evaluate one provenance table.",
                error=exc,
                metadata={"label": spec.label, "prompt_id": spec.prompt_id},
            )
            tables.append(
                {
                    "table_name": spec.table_name,
                    "label": spec.label,
                    "status": "load_failed",
                    "row_count": 0,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc)[:500],
                    "policy_boundary": {
                        "audit_only": True,
                        "broker_execution_allowed": False,
                        "policy_auto_promotion_allowed": False,
                    },
                }
            )
    issue_statuses = {"missing_table", "missing_columns", "rows_missing_required_metadata", "forbidden_authority_detected", "load_failed"}
    issue_tables = [row for row in tables if row.get("status") in issue_statuses]
    forbidden_authority_tables = [row for row in tables if int(row.get("authority_violation_count") or 0) > 0]
    return {
        "status": "issues_found" if issue_tables else "ok",
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "lookback_days": int(lookback_days),
        "limit_per_table": int(limit_per_table),
        "table_count": len(tables),
        "issue_table_count": len(issue_tables),
        "forbidden_authority_table_count": len(forbidden_authority_tables),
        "forbidden_authority_row_count": sum(int(row.get("authority_violation_count") or 0) for row in tables),
        "tables": tables,
        "policy_boundary": {
            "audit_only": True,
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        },
    }


def format_text(payload: dict[str, Any]) -> str:
    lines = [
        "LLM Provenance Audit",
        f"Status: {payload.get('status')}",
        f"Tables: {payload.get('table_count')} issue_tables={payload.get('issue_table_count')}",
        f"Lookback days: {payload.get('lookback_days')} limit/table={payload.get('limit_per_table')}",
        "",
    ]
    for row in payload.get("tables") or []:
        lines.append(
            f"- {row.get('label')} ({row.get('table_name')}): status={row.get('status')} rows={row.get('row_count')}"
        )
        if row.get("missing_columns"):
            lines.append(f"  missing_columns={','.join(row.get('missing_columns') or [])}")
        if row.get("missing_counts"):
            compact = {key: value for key, value in row.get("missing_counts", {}).items() if int(value or 0) > 0}
            if compact:
                lines.append(f"  missing_metadata={compact}")
        if row.get("evidence_missing_count"):
            lines.append(f"  evidence_missing_count={row.get('evidence_missing_count')}")
        if row.get("prompt_mismatch_count"):
            lines.append(f"  prompt_mismatch_count={row.get('prompt_mismatch_count')}")
        if row.get("authority_violation_count"):
            lines.append(
                f"  forbidden_authority_rows={row.get('authority_violation_count')} columns={row.get('authority_violation_columns') or {}}"
            )
        if row.get("error"):
            lines.append(f"  error={row.get('error_type')}: {row.get('error')}")
    lines.append("")
    lines.append("Policy boundary: read-only audit; no broker execution and no policy auto-promotion.")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit persisted LLM/Codex-derived signals for prompt/schema/evidence provenance.")
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--limit-per-table", type=int, default=500)
    parser.add_argument("--format", choices=["json", "text"], default="text")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_llm_provenance_audit(lookback_days=max(0, int(args.lookback_days)), limit_per_table=max(1, int(args.limit_per_table)))
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
