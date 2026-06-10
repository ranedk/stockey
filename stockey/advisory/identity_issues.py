from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from utils.db import db_session, sql_to_df


IDENTITY_ISSUES_TABLE = "advisory_identity_issues"


def _text(value: Any) -> str | None:
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    text = str(value).strip()
    return text or None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def ensure_identity_issues_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
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
            """
        )
        cur.execute(f"ALTER TABLE {IDENTITY_ISSUES_TABLE} ADD COLUMN IF NOT EXISTS source TEXT")
        cur.execute(f"ALTER TABLE {IDENTITY_ISSUES_TABLE} ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 1")
        cur.execute(f"ALTER TABLE {IDENTITY_ISSUES_TABLE} ADD COLUMN IF NOT EXISTS resolution_error_text TEXT")
        cur.execute(f"ALTER TABLE {IDENTITY_ISSUES_TABLE} ADD COLUMN IF NOT EXISTS resolution_context_json TEXT")
        cur.execute(f"ALTER TABLE {IDENTITY_ISSUES_TABLE} ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ")
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{IDENTITY_ISSUES_TABLE}_open
                ON {IDENTITY_ISSUES_TABLE} (status, last_seen_at DESC)
            """
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
    symbol = (_text(row.get("symbol")) or "").upper()
    requested_exchange = (_text(row.get("requested_exchange")) or "NSE").upper()
    asset_type = (_text(row.get("asset_type")) or "stock").lower()
    if not issue_key or not symbol:
        return {"issue_key": issue_key, "symbol": symbol, "status": "skipped", "reason": "missing_issue_key_or_symbol"}

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
