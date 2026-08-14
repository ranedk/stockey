from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from utils.fallback_telemetry import record_local_fallback_event
from utils.company_master import load_company_master_records
from utils.db import db_session, execute_db_operation, sql_to_df
from utils.schema_migrations import apply_schema_migration


IDENTITY_ISSUES_TABLE = "advisory_identity_issues"
IDENTITY_ISSUES_SCHEMA_MIGRATION_ID = "20260611_advisory_identity_issues_base"
IDENTITY_ISSUES_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {IDENTITY_ISSUES_TABLE} (
        issue_key TEXT PRIMARY KEY,
        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        status TEXT NOT NULL DEFAULT 'open',
        issue_type TEXT NOT NULL,
        symbol TEXT,
        requested_exchange TEXT,
        asset_type TEXT,
        company_master_id TEXT,
        source TEXT,
        error_text TEXT,
        exchanges_tried_json TEXT,
        fallback_tried_json TEXT,
        suggested_action TEXT,
        context_json TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 1,
        resolution_error_text TEXT,
        resolution_context_json TEXT,
        resolved_at TIMESTAMPTZ,
        load_ts TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"ALTER TABLE {IDENTITY_ISSUES_TABLE} ADD COLUMN IF NOT EXISTS source TEXT",
    f"ALTER TABLE {IDENTITY_ISSUES_TABLE} ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 1",
    f"ALTER TABLE {IDENTITY_ISSUES_TABLE} ADD COLUMN IF NOT EXISTS resolution_error_text TEXT",
    f"ALTER TABLE {IDENTITY_ISSUES_TABLE} ADD COLUMN IF NOT EXISTS resolution_context_json TEXT",
    f"ALTER TABLE {IDENTITY_ISSUES_TABLE} ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ",
    f"""
    CREATE INDEX IF NOT EXISTS idx_{IDENTITY_ISSUES_TABLE}_open
        ON {IDENTITY_ISSUES_TABLE} (status, last_seen_at DESC)
    """,
]


def _text(value: Any) -> str | None:
    try:
        if pd.isna(value):
            return None
    except Exception as exc:
        record_local_fallback_event(
            module="utils.identity_issues",
            fallback_type="identity_issue_text_missing_check_failed",
            source="text",
            severity="warn",
            reason="Identity issue normalization could not evaluate missingness while normalizing text and kept string conversion fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    text = str(value).strip()
    return text or None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def ensure_identity_issues_table() -> None:
    apply_schema_migration(
        migration_id=IDENTITY_ISSUES_SCHEMA_MIGRATION_ID,
        description="Create Dhan/security identity issue tracking table.",
        statements=IDENTITY_ISSUES_SCHEMA_STATEMENTS,
        metadata={"tables": [IDENTITY_ISSUES_TABLE]},
    )


def record_dhan_identity_issue(
    *,
    symbol: str,
    requested_exchange: str,
    asset_type: str = "stock",
    company: pd.Series | dict[str, Any] | None = None,
    fallback_tried: list[dict[str, Any]] | None = None,
    source: str = "resolve_dhan_identity",
    error_text: str | None = None,
) -> dict[str, Any]:
    symbol_text = (_text(symbol) or "").upper()
    exchange_text = (_text(requested_exchange) or "").upper()
    asset_type_text = (_text(asset_type) or "stock").lower()
    company_payload = dict(company) if company is not None else {}
    company_master_id = _text(company_payload.get("company_master_id"))
    exchanges_tried = [exchange_text]
    for fallback in fallback_tried or []:
        fallback_exchange = (_text(fallback.get("exchange")) or "").upper()
        if fallback_exchange and fallback_exchange not in exchanges_tried:
            exchanges_tried.append(fallback_exchange)
    issue_key = f"dhan_security_id_missing:{asset_type_text}:{exchange_text}:{symbol_text}"
    suggested_action = (
        f"Refresh Dhan scrip master and company master, then verify {exchange_text}:{symbol_text} mapping. "
        f"Run `python -m data.dhanlive.scrip_master` and the company master sync before rerunning the failed downloader/advisory step."
    )
    row = {
        "issue_key": issue_key,
        "issue_type": "dhan_security_id_missing",
        "symbol": symbol_text,
        "requested_exchange": exchange_text,
        "asset_type": asset_type_text,
        "company_master_id": company_master_id,
        "source": source,
        "error_text": error_text or f"No Dhan security id mapped for {exchange_text}:{symbol_text}",
        "exchanges_tried_json": _json_dumps(exchanges_tried),
        "fallback_tried_json": _json_dumps(fallback_tried or []),
        "suggested_action": suggested_action,
        "context_json": _json_dumps({"company_master": company_payload}),
    }
    ensure_identity_issues_table()

    def _upsert_issue() -> None:
        with db_session() as (_, cur):
            cur.execute(
                f"""
                INSERT INTO {IDENTITY_ISSUES_TABLE} (
                    issue_key,
                    first_seen_at,
                    last_seen_at,
                    status,
                    issue_type,
                    symbol,
                    requested_exchange,
                    asset_type,
                    company_master_id,
                    source,
                    error_text,
                    exchanges_tried_json,
                    fallback_tried_json,
                    suggested_action,
                    context_json,
                    attempt_count,
                    resolution_error_text,
                    resolution_context_json,
                    resolved_at,
                    load_ts
                )
                VALUES (
                    %(issue_key)s,
                    now(),
                    now(),
                    'open',
                    %(issue_type)s,
                    %(symbol)s,
                    %(requested_exchange)s,
                    %(asset_type)s,
                    %(company_master_id)s,
                    %(source)s,
                    %(error_text)s,
                    %(exchanges_tried_json)s,
                    %(fallback_tried_json)s,
                    %(suggested_action)s,
                    %(context_json)s,
                    1,
                    NULL,
                    NULL,
                    NULL,
                    now()
                )
                ON CONFLICT (issue_key) DO UPDATE SET
                    last_seen_at = EXCLUDED.last_seen_at,
                    status = 'open',
                    attempt_count = COALESCE({IDENTITY_ISSUES_TABLE}.attempt_count, 0) + 1,
                    symbol = EXCLUDED.symbol,
                    requested_exchange = EXCLUDED.requested_exchange,
                    asset_type = EXCLUDED.asset_type,
                    company_master_id = EXCLUDED.company_master_id,
                    source = EXCLUDED.source,
                    error_text = EXCLUDED.error_text,
                    exchanges_tried_json = EXCLUDED.exchanges_tried_json,
                    fallback_tried_json = EXCLUDED.fallback_tried_json,
                    suggested_action = EXCLUDED.suggested_action,
                    context_json = EXCLUDED.context_json,
                    resolution_error_text = NULL,
                    resolution_context_json = NULL,
                    resolved_at = NULL,
                    load_ts = EXCLUDED.load_ts
                """,
                row,
            )

    execute_db_operation(
        _upsert_issue,
        operation_name="identity_issues:record_dhan_identity_issue",
    )
    return row


def mark_identity_issue_resolved(
    issue_key: str,
    *,
    resolution_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key = _text(issue_key)
    if not key:
        raise ValueError("issue_key is required")
    ensure_identity_issues_table()
    context_json = _json_dumps(resolution_context or {})

    def _mark_resolved() -> None:
        with db_session() as (_, cur):
            cur.execute(
                f"""
                UPDATE {IDENTITY_ISSUES_TABLE}
                SET status = 'resolved',
                    resolved_at = now(),
                    resolution_error_text = NULL,
                    resolution_context_json = %s,
                    load_ts = now()
                WHERE issue_key = %s
                """,
                (context_json, key),
            )

    execute_db_operation(
        _mark_resolved,
        operation_name="identity_issues:mark_resolved",
    )
    return {"issue_key": key, "status": "resolved", "resolution_context": resolution_context or {}}


def mark_identity_issue_resolution_failed(
    issue_key: str,
    *,
    error_text: str,
    resolution_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key = _text(issue_key)
    if not key:
        raise ValueError("issue_key is required")
    ensure_identity_issues_table()
    context_json = _json_dumps(resolution_context or {})
    error = _text(error_text) or "Resolution failed"

    def _mark_failed() -> None:
        with db_session() as (_, cur):
            cur.execute(
                f"""
                UPDATE {IDENTITY_ISSUES_TABLE}
                SET status = 'open',
                    resolution_error_text = %s,
                    resolution_context_json = %s,
                    load_ts = now()
                WHERE issue_key = %s
                """,
                (error, context_json, key),
            )

    execute_db_operation(
        _mark_failed,
        operation_name="identity_issues:mark_resolution_failed",
    )
    return {"issue_key": key, "status": "open", "error_text": error, "resolution_context": resolution_context or {}}


def load_open_identity_issues(*, limit: int = 100) -> pd.DataFrame:
    ensure_identity_issues_table()
    return sql_to_df(
        f"""
        SELECT *
        FROM {IDENTITY_ISSUES_TABLE}
        WHERE COALESCE(status, 'open') IN ('open', 'active')
        ORDER BY last_seen_at DESC NULLS LAST, first_seen_at DESC NULLS LAST
        LIMIT %s
        """,
        params=(max(1, int(limit)),),
        retries=3,
    )


def _resolve_one_issue(row: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    issue_key = _text(row.get("issue_key")) or ""
    issue_type = _text(row.get("issue_type")) or "identity_issue"
    symbol = (_text(row.get("symbol")) or "").upper()
    requested_exchange = (_text(row.get("requested_exchange")) or "NSE").upper()
    asset_type = (_text(row.get("asset_type")) or "stock").lower()
    if not issue_key or not symbol:
        return {"issue_key": issue_key, "symbol": symbol, "status": "skipped", "reason": "missing_issue_key_or_symbol"}

    if issue_type == "dhan_ohlcv_history_unavailable":
        try:
            latest = sql_to_df(
                """
                SELECT MAX(date) AS latest_date
                FROM dhan_ohlcv_daily
                WHERE UPPER(TRIM(ticker)) = %s
                  AND UPPER(TRIM(exchange)) = %s
                  AND LOWER(TRIM(asset_type)) = %s
                """,
                params=(symbol, requested_exchange, asset_type),
                retries=3,
            )
            latest_date = pd.to_datetime(latest["latest_date"].iloc[0], utc=True, errors="coerce") if not latest.empty else pd.NaT
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            context = {"checked_symbol": symbol, "requested_exchange": requested_exchange, "asset_type": asset_type}
            record_local_fallback_event(
                module="utils.identity_issues",
                fallback_type="ohlcv_history_issue_resolution_failed",
                source="dhan_ohlcv_daily",
                severity="warn",
                symbol=symbol or None,
                reason="Open Dhan OHLCV history issue could not be checked and remains open.",
                error=exc,
                metadata={"issue_key": issue_key, "apply": bool(apply)},
            )
            if apply:
                mark_identity_issue_resolution_failed(issue_key, error_text=error_text, resolution_context=context)
            return {
                "issue_key": issue_key,
                "symbol": symbol,
                "requested_exchange": requested_exchange,
                "status": "still_open",
                "error": error_text,
                "applied": bool(apply),
            }
        if pd.isna(latest_date):
            return {
                "issue_key": issue_key,
                "symbol": symbol,
                "requested_exchange": requested_exchange,
                "status": "still_open",
                "reason": "dhan_daily_history_still_missing",
                "suggested_action": "Repair Dhan OHLCV availability or explicitly exclude this symbol from context-watch intake.",
                "applied": bool(apply),
            }
        context = {
            "checked_symbol": symbol,
            "requested_exchange": requested_exchange,
            "asset_type": asset_type,
            "latest_ohlcv_date": latest_date.isoformat(),
        }
        if apply:
            mark_identity_issue_resolved(issue_key, resolution_context=context)
        return {
            "issue_key": issue_key,
            "symbol": symbol,
            "requested_exchange": requested_exchange,
            "status": "resolved" if apply else "would_resolve",
            "applied": bool(apply),
            "latest_ohlcv_date": latest_date.isoformat(),
        }

    if issue_type == "company_master_mapping_missing":
        try:
            records = load_company_master_records(symbol, exchanges=[requested_exchange])
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            context = {"checked_symbol": symbol, "requested_exchange": requested_exchange}
            record_local_fallback_event(
                module="utils.identity_issues",
                fallback_type="company_master_mapping_issue_resolution_failed",
                source="company_master",
                severity="warn",
                symbol=symbol or None,
                reason="Open company-master mapping issue could not be checked and remains open.",
                error=exc,
                metadata={"issue_key": issue_key, "apply": bool(apply)},
            )
            if apply:
                mark_identity_issue_resolution_failed(issue_key, error_text=error_text, resolution_context=context)
            return {
                "issue_key": issue_key,
                "symbol": symbol,
                "requested_exchange": requested_exchange,
                "status": "still_open",
                "error": error_text,
                "applied": bool(apply),
            }
        if records.empty:
            return {
                "issue_key": issue_key,
                "symbol": symbol,
                "requested_exchange": requested_exchange,
                "status": "still_open",
                "reason": "company_master_mapping_still_missing",
                "suggested_action": "Refresh company master inputs and verify this ticker/exchange maps to a company_master_id.",
                "applied": bool(apply),
            }
        first = records.iloc[0].to_dict()
        context = {
            "checked_symbol": symbol,
            "requested_exchange": requested_exchange,
            "company_master_id": _text(first.get("company_master_id")),
            "matched_rows": int(len(records)),
        }
        if apply:
            mark_identity_issue_resolved(issue_key, resolution_context=context)
        return {
            "issue_key": issue_key,
            "symbol": symbol,
            "requested_exchange": requested_exchange,
            "status": "resolved" if apply else "would_resolve",
            "applied": bool(apply),
            "company_master_id": context["company_master_id"],
            "matched_rows": context["matched_rows"],
        }

    from data.dhanlive import dhan_db

    original_record_issue = getattr(dhan_db, "record_dhan_identity_issue", None)
    original_record_fallback = getattr(dhan_db, "record_fallback_event", None)
    if not apply:
        dhan_db.record_dhan_identity_issue = lambda **kwargs: kwargs
        dhan_db.record_fallback_event = lambda **kwargs: kwargs
    try:
        identity = dhan_db.resolve_dhan_identity(symbol, requested_exchange, asset_type=asset_type)
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        context = {"checked_symbol": symbol, "requested_exchange": requested_exchange, "asset_type": asset_type}
        record_local_fallback_event(
            module="utils.identity_issues",
            fallback_type="identity_issue_resolution_failed",
            source="data.dhanlive.dhan_db.resolve_dhan_identity",
            severity="warn",
            symbol=symbol or None,
            reason="Open Dhan/security identity issue could not be resolved and remains open.",
            error=exc,
            metadata={
                "issue_key": issue_key,
                "requested_exchange": requested_exchange,
                "asset_type": asset_type,
                "apply": bool(apply),
            },
        )
        if apply:
            mark_identity_issue_resolution_failed(issue_key, error_text=error_text, resolution_context=context)
        return {
            "issue_key": issue_key,
            "symbol": symbol,
            "requested_exchange": requested_exchange,
            "status": "still_open",
            "error": error_text,
            "applied": bool(apply),
        }
    finally:
        if not apply:
            if original_record_issue is not None:
                dhan_db.record_dhan_identity_issue = original_record_issue
            if original_record_fallback is not None:
                dhan_db.record_fallback_event = original_record_fallback

    context = {
        "checked_symbol": symbol,
        "requested_exchange": requested_exchange,
        "asset_type": asset_type,
        "resolved_identity": identity,
    }
    if apply:
        mark_identity_issue_resolved(issue_key, resolution_context=context)
    return {
        "issue_key": issue_key,
        "symbol": symbol,
        "requested_exchange": requested_exchange,
        "status": "resolved" if apply else "would_resolve",
        "applied": bool(apply),
        "resolved_identity": identity,
    }


def resolve_open_identity_issues(*, limit: int = 100, apply: bool = False, issue_keys: list[str] | None = None) -> dict[str, Any]:
    df = load_open_identity_issues(limit=limit)
    rows = [dict(row) for row in df.to_dict(orient="records")] if not df.empty else []
    requested_keys = {_text(key) for key in issue_keys or []}
    requested_keys = {key for key in requested_keys if key}
    if requested_keys:
        rows = [row for row in rows if _text(row.get("issue_key")) in requested_keys]
    results = [_resolve_one_issue(row, apply=apply) for row in rows]
    counts: dict[str, int] = {}
    for row in results:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "status": "ok",
        "mode": "apply" if apply else "dry_run",
        "checked_rows": len(rows),
        "requested_issue_keys": sorted(requested_keys),
        "counts": counts,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recheck and optionally close open Dhan/security identity issues.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum open issues to recheck.")
    parser.add_argument("--apply", action="store_true", help="Mark issues resolved when the current Dhan/company mapping resolves.")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    args = parser.parse_args(argv)
    summary = resolve_open_identity_issues(limit=max(1, int(args.limit)), apply=bool(args.apply))
    if args.format == "json":
        print(_json_dumps(summary))
    else:
        print(f"mode={summary['mode']} checked={summary['checked_rows']} counts={summary['counts']}")
        for row in summary["results"]:
            print(f"{row.get('symbol')} {row.get('requested_exchange')} {row.get('status')} {row.get('error') or ''}".strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
