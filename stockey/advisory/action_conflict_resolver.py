from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.decision_trace import ACTION_CONFLICTS_TABLE, ACTION_CONFLICT_RULES_TABLE
from advisory.decision_trace import classify_action_conflict, ensure_trace_tables
from advisory.decision_trace import load_enabled_dynamic_action_conflict_rules
from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import db_session, execute_db_operation, sql_to_df
from utils.sync import parse_datetime_arg


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if value is None:
        return None
    if not isinstance(value, (dict, list, tuple, set)):
        try:
            if pd.isna(value):
                return None
        except Exception as exc:
            record_local_fallback_event(
                module="advisory.action_conflict_resolver",
                fallback_type="action_conflict_json_ready_missing_check_failed",
                source="json_ready",
                severity="warn",
                reason="Action conflict resolver could not evaluate missingness for a value and kept the original value.",
                error=exc,
                metadata={"value_type": type(value).__name__},
            )
    return value


def latest_asof_date() -> pd.Timestamp | None:
    ensure_trace_tables()
    df = sql_to_df(f"SELECT MAX(asof_date) AS asof_date FROM {ACTION_CONFLICTS_TABLE}", retries=2)
    if df.empty:
        return None
    value = pd.to_datetime(df["asof_date"].iloc[0], utc=True, errors="coerce")
    return None if pd.isna(value) else value


def load_enabled_rules() -> list[dict[str, Any]]:
    ensure_trace_tables()
    df = sql_to_df(
        f"""
        SELECT *
        FROM {ACTION_CONFLICT_RULES_TABLE}
        WHERE COALESCE(enabled, TRUE)
        ORDER BY priority DESC, rule_id
        """,
        retries=2,
    )
    return df.to_dict(orient="records") if not df.empty else []


def load_conflicts(*, asof_date: pd.Timestamp | None, symbol: str | None, unresolved_only: bool) -> pd.DataFrame:
    ensure_trace_tables()
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if asof_date is not None:
        clauses.append("asof_date = %(asof_date)s")
        params["asof_date"] = pd.to_datetime(asof_date, utc=True, errors="coerce").to_pydatetime()
    if symbol:
        clauses.append("UPPER(symbol) = %(symbol)s")
        params["symbol"] = str(symbol).strip().upper()
    if unresolved_only:
        clauses.append("(resolution_status IS NULL OR resolution_status <> 'resolved' OR COALESCE(requires_manual_resolution, FALSE))")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return sql_to_df(
        f"""
        SELECT ctid::text AS row_id, *
        FROM {ACTION_CONFLICTS_TABLE}
        {where}
        ORDER BY asof_date DESC, symbol, load_ts DESC
        """,
        params=params,
        retries=2,
    )


def dedupe_conflicts(*, asof_date: pd.Timestamp | None, symbol: str | None, dry_run: bool) -> int:
    clauses = []
    params: dict[str, Any] = {}
    if asof_date is not None:
        clauses.append("asof_date = %(asof_date)s")
        params["asof_date"] = pd.to_datetime(asof_date, utc=True, errors="coerce").to_pydatetime()
    if symbol:
        clauses.append("UPPER(symbol) = %(symbol)s")
        params["symbol"] = str(symbol).strip().upper()
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    delete_sql = f"""
        DELETE FROM {ACTION_CONFLICTS_TABLE} t
        USING (
            SELECT ctid
            FROM (
                SELECT ctid,
                       ROW_NUMBER() OVER (
                           PARTITION BY asof_date, symbol, winning_action_code, losing_action_code, winning_source, losing_source, losing_setup_id, losing_unique_id
                           ORDER BY load_ts DESC NULLS LAST, ctid DESC
                       ) AS rn
                FROM {ACTION_CONFLICTS_TABLE}
                {where}
            ) ranked
            WHERE rn > 1
        ) d
        WHERE t.ctid = d.ctid
    """
    count_sql = f"""
        SELECT COUNT(*) AS count
        FROM (
            SELECT ROW_NUMBER() OVER (
                       PARTITION BY asof_date, symbol, winning_action_code, losing_action_code, winning_source, losing_source, losing_setup_id, losing_unique_id
                       ORDER BY load_ts DESC NULLS LAST, ctid DESC
                   ) AS rn
            FROM {ACTION_CONFLICTS_TABLE}
            {where}
        ) ranked
        WHERE rn > 1
    """
    duplicate_count = int(sql_to_df(count_sql, params=params, retries=2)["count"].iloc[0])
    if dry_run or duplicate_count <= 0:
        return duplicate_count

    def _delete_duplicates() -> int:
        with db_session() as (_, cur):
            cur.execute(delete_sql, params)
            return int(cur.rowcount or 0)

    return execute_db_operation(
        _delete_duplicates,
        operation_name="action_conflict_resolver:dedupe_conflicts",
    )


def resolve_conflicts(
    *,
    asof_date: pd.Timestamp | None = None,
    symbol: str | None = None,
    unresolved_only: bool = False,
    dry_run: bool = False,
    dedupe: bool = True,
) -> dict[str, Any]:
    ensure_trace_tables()
    effective_asof = asof_date
    if effective_asof is None:
        effective_asof = latest_asof_date()
    duplicate_rows = dedupe_conflicts(asof_date=effective_asof, symbol=symbol, dry_run=dry_run) if dedupe else 0
    conflicts = load_conflicts(asof_date=effective_asof, symbol=symbol, unresolved_only=unresolved_only)
    rules = load_enabled_rules()
    dynamic_rules = load_enabled_dynamic_action_conflict_rules()
    updates: list[dict[str, Any]] = []
    for row in conflicts.to_dict(orient="records"):
        resolution = classify_action_conflict(row, dynamic_rules=dynamic_rules)
        updates.append(
            {
                "row_id": row["row_id"],
                "symbol": row.get("symbol"),
                "winning_action_code": row.get("winning_action_code"),
                "losing_action_code": row.get("losing_action_code"),
                **resolution,
            }
        )

    if not dry_run and updates:
        def _persist_resolutions() -> None:
            with db_session() as (_, cur):
                for item in updates:
                    cur.execute(
                        f"""
                        UPDATE {ACTION_CONFLICTS_TABLE}
                        SET resolution_status = %s,
                            resolution_rule_id = %s,
                            resolution_action = %s,
                            resolution_reason = %s,
                            requires_manual_resolution = %s
                        WHERE ctid = %s::tid
                        """,
                        (
                            item.get("resolution_status"),
                            item.get("resolution_rule_id"),
                            item.get("resolution_action"),
                            item.get("resolution_reason"),
                            item.get("requires_manual_resolution"),
                            item["row_id"],
                        ),
                    )

        execute_db_operation(
            _persist_resolutions,
            operation_name="action_conflict_resolver:persist_resolutions",
        )

    status_counts: dict[str, int] = {}
    rule_counts: dict[str, int] = {}
    manual_required = 0
    for item in updates:
        status = str(item.get("resolution_status") or "unknown")
        rule = str(item.get("resolution_rule_id") or "UNRESOLVED")
        status_counts[status] = status_counts.get(status, 0) + 1
        rule_counts[rule] = rule_counts.get(rule, 0) + 1
        manual_required += int(bool(item.get("requires_manual_resolution")))

    sample = [
        {key: _json_ready(item.get(key)) for key in ["symbol", "winning_action_code", "losing_action_code", "resolution_status", "resolution_rule_id", "resolution_action", "requires_manual_resolution"]}
        for item in updates[:10]
    ]
    return {
        "status": "ok",
        "dry_run": bool(dry_run),
        "asof_date": None if effective_asof is None else pd.to_datetime(effective_asof, utc=True, errors="coerce").isoformat(),
        "symbol": None if not symbol else str(symbol).strip().upper(),
        "conflict_rows": int(len(conflicts)),
        "duplicate_rows": int(duplicate_rows),
        "updated_rows": 0 if dry_run else int(len(updates)),
        "enabled_rules": int(len(rules)),
        "status_counts": status_counts,
        "rule_counts": rule_counts,
        "manual_required": int(manual_required),
        "sample": sample,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve advisory action conflicts using enabled deterministic conflict rules.")
    parser.add_argument("--asof-date", help="Conflict as-of date. Use latest when omitted.")
    parser.add_argument("--symbol", help="Optional symbol to resolve.")
    parser.add_argument("--unresolved-only", action="store_true", help="Only process unresolved/manual-required conflicts.")
    parser.add_argument("--no-dedupe", action="store_true", help="Do not delete duplicate conflict rows before resolving.")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing updates.")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof_date = parse_datetime_arg(args.asof_date, name="asof_date") if args.asof_date else None
    summary = resolve_conflicts(
        asof_date=asof_date,
        symbol=args.symbol,
        unresolved_only=bool(args.unresolved_only),
        dry_run=bool(args.dry_run),
        dedupe=not bool(args.no_dedupe),
    )
    if args.format == "json":
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"status={summary['status']} asof={summary['asof_date']} conflicts={summary['conflict_rows']} updated={summary['updated_rows']} dry_run={summary['dry_run']}")
        print(f"rules={summary['rule_counts']} manual_required={summary['manual_required']} duplicates={summary['duplicate_rows']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
